-- gba-shadow bridge for mGBA (0.10+).
--
-- Streams the GBA's display state (I/O registers, palette, VRAM, OAM) to the
-- gba-shadow renderer once per frame over TCP, plus any work-RAM ranges the
-- renderer asks for (used by game adapters to read entity state). It can also
-- write snapshots + reference screenshots to disk for verifying the renderer.
--
-- Load it in mGBA via Tools > Scripting... > File > Load script.
--
-- Settings can be edited below or overridden with environment variables
-- (SHADOW_HOST, SHADOW_PORT, SHADOW_STREAM, SHADOW_CAPTURE_DIR,
--  SHADOW_CAPTURE_EVERY, SHADOW_CAPTURE_COUNT, SHADOW_CAPTURE_START,
--  SHADOW_WATCH, SHADOW_RAM_DELAY).

local CONFIG = {
    host = "127.0.0.1",
    port = 61337,
    stream = true,           -- send frames to the renderer
    reconnect_every = 60,    -- frames between connection attempts
    capture_dir = nil,       -- e.g. "/tmp/captures" to dump snapshot + screenshot pairs
    capture_every = 30,      -- frames between captures
    capture_count = 20,      -- stop after this many captures (0 = unlimited)
    capture_start = 0,       -- first frame eligible for capture
    -- RAM ranges to stream when no renderer has asked for any (e.g. for captures):
    -- "0x02037350:0x264,0x03005d8c:0x64". A connected renderer replaces this list.
    watch = nil,
    ram_delay = 0,           -- frames to delay RAM by (see README: "timing")
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
CONFIG.watch = env("SHADOW_WATCH") or CONFIG.watch
CONFIG.ram_delay = env("SHADOW_RAM_DELAY", tonumber) or CONFIG.ram_delay

-- Wire format (little endian), shared with shadow/protocol.py:
--   header: "GBSH" u8 version u8 flags u16 record_count u32 frame u32 payload_len
--   record: u8 region u32 offset u32 length, then `length` bytes
-- flags bit0 = full snapshot, bit1 = hello (payload: s1 game code, s1 title, no records).
-- Renderer -> bridge: "GBSW" u8 version u8 flags(bit0: delay RAM 1 frame) u16 count,
--   then count x (u32 bus address, u32 length).
local MAGIC, VERSION = "GBSH", 1
local FLAG_FULL, FLAG_HELLO = 1, 2
local WATCH_MAGIC = "GBSW"

-- Display state: region id, memory domain name, bytes to mirror, diff chunk size.
local VIDEO = {
    { key = "io",  id = 0, domain = "io",      base = 0, size = 0x60,    chunk = 0x60 },
    { key = "pal", id = 1, domain = "palette", base = 0, size = 0x400,   chunk = 0x400 },
    { key = "vram",id = 2, domain = "vram",    base = 0, size = 0x18000, chunk = 0x400 },
    { key = "oam", id = 3, domain = "oam",     base = 0, size = 0x400,   chunk = 0x400 },
}
-- Work RAM regions that may be watched: region id, domain, bus base, size.
local RAM = {
    { id = 4, domain = "wram",  bus = 0x02000000, size = 0x40000 },
    { id = 5, domain = "iwram", bus = 0x03000000, size = 0x8000 },
}
local RAM_CHUNK = 0x100

local sock = nil
local last_sent = {}      -- [block key][chunk index] = string last sent to the renderer
local pending = nil       -- unsent tail of a message (socket would block)
local inbuf = ""          -- bytes received from the renderer, not yet parsed
local frames_since_attempt = CONFIG.reconnect_every
local captures_done = 0
local watch_blocks = {}   -- RAM blocks currently streamed
local ram_prev = nil      -- RAM chunks read on the previous frame (for ram_delay)
local ram_delay = 0

local function log(msg) console:log("[shadow] " .. msg) end

-- Turn a list of {address, length} into RAM blocks (one per range, split by region).
local function set_watch(ranges, delay)
    watch_blocks = {}
    for i, r in ipairs(ranges) do
        local addr, len = r[1], r[2]
        local ok = false
        for _, reg in ipairs(RAM) do
            if addr >= reg.bus and addr + len <= reg.bus + reg.size and len > 0 then
                watch_blocks[#watch_blocks + 1] = {
                    key = "ram" .. i, id = reg.id, domain = reg.domain,
                    base = addr - reg.bus, size = len, chunk = RAM_CHUNK,
                }
                ok = true
            end
        end
        if not ok then log(string.format("ignoring watch range %08X+%X (not in EWRAM/IWRAM)", addr, len)) end
    end
    ram_delay = delay or 0
    ram_prev = nil
    for k in pairs(last_sent) do
        if string.sub(k, 1, 3) == "ram" then last_sent[k] = nil end
    end
    log(string.format("watching %d RAM range(s), delay %d frame(s)", #watch_blocks, ram_delay))
end

local function parse_watch_config(text)
    local ranges = {}
    for a, l in string.gmatch(text, "(0?x?%x+):(0?x?%x+)") do
        ranges[#ranges + 1] = { tonumber(a), tonumber(l) }
    end
    return ranges
end

local function read_blocks(blocks)
    local out = {}
    for _, b in ipairs(blocks) do
        local dom = emu.memory[b.domain]
        local chunks = {}
        for off = 0, b.size - 1, b.chunk do
            chunks[#chunks + 1] = dom:readRange(b.base + off, math.min(b.chunk, b.size - off))
        end
        out[b.key] = chunks
    end
    return out
end

-- Read the frame's state: video now, RAM now or from the previous frame.
local function read_state()
    local state = read_blocks(VIDEO)
    local blocks = VIDEO
    if #watch_blocks > 0 then
        local ram_now = read_blocks(watch_blocks)
        local ram = ram_now
        if ram_delay > 0 then
            ram = ram_prev
            ram_prev = ram_now
        end
        if ram then
            for k, v in pairs(ram) do state[k] = v end
            blocks = {}
            for _, b in ipairs(VIDEO) do blocks[#blocks + 1] = b end
            for _, b in ipairs(watch_blocks) do blocks[#blocks + 1] = b end
        end
    end
    return state, blocks
end

-- Build a message. If `full`, every chunk is included; otherwise only chunks
-- that differ from `last_sent`. Adjacent changed chunks are merged.
local function build_message(state, blocks, frame, full)
    local parts, count = {}, 0
    for _, b in ipairs(blocks) do
        local chunks = state[b.key]
        local prev = last_sent[b.key] or {}
        local run_start, run = nil, nil
        local function flush()
            if run_start then
                local data = table.concat(run)
                parts[#parts + 1] = string.pack("<BI4I4", b.id, b.base + run_start, #data)
                parts[#parts + 1] = data
                count = count + 1
                run_start, run = nil, nil
            end
        end
        for i, c in ipairs(chunks) do
            if full or prev[i] ~= c then
                if not run_start then run_start, run = (i - 1) * b.chunk, {} end
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
    for key, chunks in pairs(state) do last_sent[key] = chunks end
end

local function disconnect(reason)
    if sock then
        sock:close()
        sock = nil
        log("disconnected: " .. tostring(reason))
        -- Forget what this renderer asked for; fall back to the configured list.
        set_watch(CONFIG.watch and parse_watch_config(CONFIG.watch) or {}, CONFIG.ram_delay)
    end
    pending = nil
    inbuf = ""
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

local function send_hello()
    local code = emu:getGameCode() or ""
    code = string.match(code, "(%w%w%w%w)$") or code   -- "AGB-BPEE" -> "BPEE"
    local title = emu:getGameTitle() or ""
    local payload = string.pack("<s1s1", code, title)
    local msg = string.pack("<c4BBHI4I4", MAGIC, VERSION, FLAG_HELLO, 0, emu:currentFrame(), #payload) .. payload
    local rest = push(msg)
    if rest and rest ~= "" then pending = rest end
    log(string.format("sent hello: game code %s, title %q", code, title))
end

local function try_connect()
    frames_since_attempt = 0
    local s = socket.connect(CONFIG.host, CONFIG.port)
    if s then
        sock = s
        last_sent = {}
        pending = nil
        inbuf = ""
        log(string.format("connected to renderer at %s:%d", CONFIG.host, CONFIG.port))
        send_hello()
    end
end

-- Handle watch requests from the renderer.
local function poll_incoming()
    while sock and sock:hasdata() do
        local data, err = sock:receive(4096)
        if not data then
            disconnect(err)
            return
        end
        inbuf = inbuf .. data
    end
    while #inbuf >= 8 do
        local magic, _, flags, count = string.unpack("<c4BBH", inbuf)
        if magic ~= WATCH_MAGIC then
            disconnect("bad message from renderer")
            return
        end
        local need = 8 + count * 8
        if #inbuf < need then return end
        local ranges = {}
        for i = 0, count - 1 do
            local addr, len = string.unpack("<I4I4", inbuf, 9 + i * 8)
            ranges[#ranges + 1] = { addr, len }
        end
        inbuf = string.sub(inbuf, need + 1)
        set_watch(ranges, (flags & 1 == 1) and 1 or 0)
    end
end

local function stream(state, blocks, frame)
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
        return
    end
    local full = last_sent["io"] == nil
    local msg = build_message(state, blocks, frame, full)
    local rest = push(msg)
    if rest == nil then return end
    remember(state)
    if rest ~= "" then pending = rest end
end

local function capture(state, blocks, frame)
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
    f:write(build_message(state, blocks, frame, true))
    f:close()
    emu:screenshot(base .. ".png")
    captures_done = captures_done + 1
end

local function on_frame()
    if sock then poll_incoming() end
    local frame = emu:currentFrame()
    local need_stream = CONFIG.stream
    local need_capture = CONFIG.capture_dir ~= nil
    if not need_stream and not need_capture then return end
    local state, blocks = read_state()
    if need_capture then capture(state, blocks, frame) end
    if need_stream then stream(state, blocks, frame) end
end

local function on_reset()
    -- Force a full snapshot on the next frame.
    last_sent = {}
    ram_prev = nil
end

if CONFIG.watch then set_watch(parse_watch_config(CONFIG.watch), CONFIG.ram_delay) end
callbacks:add("frame", on_frame)
callbacks:add("reset", on_reset)
callbacks:add("start", on_reset)
log(string.format("bridge loaded (stream=%s, capture_dir=%s)", tostring(CONFIG.stream), tostring(CONFIG.capture_dir)))
