-- gba-shadow bridge for mGBA (0.10+).
--
-- Streams the GBA's display state (I/O registers, palette, VRAM, OAM) to the
-- gba-shadow renderer once per frame over TCP, and can optionally write
-- snapshots + reference screenshots to disk for verifying the renderer.
--
-- Load it in mGBA via Tools > Scripting... > File > Load script.
--
-- Settings can be edited below or overridden with environment variables
-- (SHADOW_HOST, SHADOW_PORT, SHADOW_STREAM, SHADOW_CAPTURE_DIR,
--  SHADOW_CAPTURE_EVERY, SHADOW_CAPTURE_COUNT, SHADOW_CAPTURE_START).

local CONFIG = {
    host = "127.0.0.1",
    port = 61337,
    stream = true,           -- send frames to the renderer
    reconnect_every = 60,    -- frames between connection attempts
    capture_dir = nil,       -- e.g. "/tmp/captures" to dump snapshot + screenshot pairs
    capture_every = 30,      -- frames between captures
    capture_count = 20,      -- stop after this many captures (0 = unlimited)
    capture_start = 0,       -- first frame eligible for capture
}

local function env(name, conv)
    local v = os.getenv(name)
    if v == nil or v == "" then return nil end
    return conv and conv(v) or v
end
CONFIG.host = env("SHADOW_HOST") or CONFIG.host
CONFIG.port = env("SHADOW_PORT", tonumber) or CONFIG.port
local stream_env = env("SHADOW_STREAM")
if stream_env ~= nil then CONFIG.stream = stream_env ~= "0" end
CONFIG.capture_dir = env("SHADOW_CAPTURE_DIR") or CONFIG.capture_dir
CONFIG.capture_every = env("SHADOW_CAPTURE_EVERY", tonumber) or CONFIG.capture_every
CONFIG.capture_count = env("SHADOW_CAPTURE_COUNT", tonumber) or CONFIG.capture_count
CONFIG.capture_start = env("SHADOW_CAPTURE_START", tonumber) or CONFIG.capture_start

-- Wire format (little endian), shared with shadow/protocol.py:
--   header: "GBSH" u8 version u8 flags u16 record_count u32 frame u32 payload_len
--   record: u8 region u32 offset u32 length, then `length` bytes
-- flags bit0 = full snapshot (receiver should treat it as a complete state).
local MAGIC, VERSION = "GBSH", 1
local FLAG_FULL = 1

-- region id, memory domain name, bytes to mirror, diff chunk size
local REGIONS = {
    { id = 0, domain = "io",      size = 0x60,    chunk = 0x60 },
    { id = 1, domain = "palette", size = 0x400,   chunk = 0x400 },
    { id = 2, domain = "vram",    size = 0x18000, chunk = 0x400 },
    { id = 3, domain = "oam",     size = 0x400,   chunk = 0x400 },
}

local sock = nil
local last_sent = {}      -- [region id][chunk index] = string last sent to the renderer
local pending = nil       -- unsent tail of a message (socket would block)
local frames_since_attempt = CONFIG.reconnect_every
local captures_done = 0
local stats = { sent = 0, dropped = 0 }

local function log(msg) console:log("[shadow] " .. msg) end

-- Read every region; returns { [id] = { chunks... } }.
local function read_state()
    local state = {}
    for _, r in ipairs(REGIONS) do
        local dom = emu.memory[r.domain]
        local chunks = {}
        for off = 0, r.size - 1, r.chunk do
            chunks[#chunks + 1] = dom:readRange(off, math.min(r.chunk, r.size - off))
        end
        state[r.id] = chunks
    end
    return state
end

-- Build a message. If `full`, every chunk is included; otherwise only chunks
-- that differ from `last_sent`. Adjacent changed chunks are merged.
local function build_message(state, frame, full)
    local parts, count = {}, 0
    for _, r in ipairs(REGIONS) do
        local chunks = state[r.id]
        local prev = last_sent[r.id] or {}
        local run_start, run = nil, nil
        local function flush()
            if run_start then
                local data = table.concat(run)
                parts[#parts + 1] = string.pack("<BI4I4", r.id, run_start, #data)
                parts[#parts + 1] = data
                count = count + 1
                run_start, run = nil, nil
            end
        end
        for i, c in ipairs(chunks) do
            if full or prev[i] ~= c then
                if not run_start then run_start, run = (i - 1) * r.chunk, {} end
                run[#run + 1] = c
            else
                flush()
            end
        end
        flush()
    end
    local payload = table.concat(parts)
    local header = string.pack("<c4BBHI4I4", MAGIC, VERSION, full and FLAG_FULL or 0, count, frame, #payload)
    return header .. payload
end

local function remember(state)
    for id, chunks in pairs(state) do last_sent[id] = chunks end
end

local function disconnect(reason)
    if sock then
        sock:close()
        sock = nil
        log("disconnected: " .. tostring(reason))
    end
    pending = nil
    last_sent = {}
end

-- Try to push `data`; returns the unsent remainder ("" when done) or nil on error.
local function push(data)
    while #data > 0 do
        local n, err = sock:send(data)
        if not n then
            if sock._s.error == C.SOCKERR.AGAIN then return data end
            disconnect(err)
            return nil
        end
        if n <= 0 then return data end
        data = string.sub(data, n + 1)
    end
    return ""
end

local function try_connect()
    frames_since_attempt = 0
    local s, err = socket.connect(CONFIG.host, CONFIG.port)
    if s then
        sock = s
        last_sent = {}
        pending = nil
        log(string.format("connected to renderer at %s:%d", CONFIG.host, CONFIG.port))
    end
end

local function stream(state, frame)
    if not sock then
        frames_since_attempt = frames_since_attempt + 1
        if frames_since_attempt >= CONFIG.reconnect_every then try_connect() end
        if not sock then return end
    end
    if pending then
        -- Renderer is behind: finish the old message and drop this frame.
        -- last_sent is unchanged, so the next diff is still correct.
        pending = push(pending)
        if pending == "" then pending = nil end
        stats.dropped = stats.dropped + 1
        return
    end
    local full = next(last_sent) == nil
    local msg = build_message(state, frame, full)
    local rest = push(msg)
    if rest == nil then return end
    remember(state)
    stats.sent = stats.sent + 1
    if rest ~= "" then pending = rest end
end

local function capture(state, frame)
    if not CONFIG.capture_dir then return end
    if CONFIG.capture_count > 0 and captures_done >= CONFIG.capture_count then return end
    if frame < CONFIG.capture_start or (frame - CONFIG.capture_start) % CONFIG.capture_every ~= 0 then return end
    local base = string.format("%s/frame_%06d", CONFIG.capture_dir, frame)
    local f = io.open(base .. ".gbsh", "wb")
    if not f then
        log("cannot write to capture dir " .. CONFIG.capture_dir)
        CONFIG.capture_dir = nil
        return
    end
    f:write(build_message(state, frame, true))
    f:close()
    emu:screenshot(base .. ".png")
    captures_done = captures_done + 1
end

local function on_frame()
    local frame = emu:currentFrame()
    local need_stream = CONFIG.stream
    local need_capture = CONFIG.capture_dir ~= nil
    if not need_stream and not need_capture then return end
    local state = read_state()
    if need_capture then capture(state, frame) end
    if need_stream then stream(state, frame) end
end

local function on_reset()
    -- Force a full snapshot on the next frame.
    last_sent = {}
end

callbacks:add("frame", on_frame)
callbacks:add("reset", on_reset)
callbacks:add("start", on_reset)
log(string.format("bridge loaded (stream=%s, capture_dir=%s)", tostring(CONFIG.stream), tostring(CONFIG.capture_dir)))
