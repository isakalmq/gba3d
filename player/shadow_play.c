// shadow-play: a minimal GBA player built on libmgba that loads a Lua script at
// startup, so the gba-shadow bridge runs without clicking through mGBA's
// scripting window (mGBA 0.10 has no command-line option for that).
//
//   shadow-play ROM [SCRIPT] [--scale N]
//
// Keys: arrows = D-pad, X = A, Z = B, A = L, S = R, Enter = Start,
//       Backspace = Select, Tab (hold) = fast forward, F5 = save state,
//       F8 = load state, Esc = quit. Game controllers work too.
// Battery saves are written next to the ROM (same as mGBA).
//
// Environment: SHADOW_PLAY_EXIT_AFTER=N quits after N frames (for tests).
#include <mgba/flags.h>
#include <mgba/core/blip_buf.h>
#include <mgba/core/core.h>
#include <mgba/core/log.h>
#include <mgba/core/scripting.h>
#include <mgba/core/serialize.h>
#include <mgba/internal/gba/input.h>
#include <mgba/internal/script/lua.h>
#include <mgba/script/context.h>
#include <mgba-util/vfs.h>

#include <SDL.h>

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define AUDIO_RATE 48000
#define AUDIO_MAX_QUEUED_MS 120

static void log_cb(struct mLogger* logger, int category, enum mLogLevel level, const char* fmt, va_list args) {
    (void) logger;
    if (!(level & (mLOG_FATAL | mLOG_ERROR | mLOG_WARN | mLOG_INFO))) {
        return;
    }
    if (level & (mLOG_WARN | mLOG_INFO) && category != mLogCategoryById("script")) {
        return;  // core chatter
    }
    fprintf(stderr, "[%s] ", mLogCategoryName(category));
    vfprintf(stderr, fmt, args);
    fputc('\n', stderr);
}

static void frame_ended(void* ctx) {
    mScriptContextTriggerCallback(ctx, "frame");
}

struct KeyBinding {
    SDL_Keycode key;
    int gba;
};

static const struct KeyBinding KEYS[] = {
    { SDLK_x, GBA_KEY_A },        { SDLK_z, GBA_KEY_B },
    { SDLK_BACKSPACE, GBA_KEY_SELECT }, { SDLK_RETURN, GBA_KEY_START },
    { SDLK_RIGHT, GBA_KEY_RIGHT }, { SDLK_LEFT, GBA_KEY_LEFT },
    { SDLK_UP, GBA_KEY_UP },       { SDLK_DOWN, GBA_KEY_DOWN },
    { SDLK_s, GBA_KEY_R },         { SDLK_a, GBA_KEY_L },
};

static uint32_t read_keys(SDL_GameController* pad) {
    const Uint8* state = SDL_GetKeyboardState(NULL);
    uint32_t keys = 0;
    for (size_t i = 0; i < sizeof(KEYS) / sizeof(KEYS[0]); ++i) {
        if (state[SDL_GetScancodeFromKey(KEYS[i].key)]) {
            keys |= 1u << KEYS[i].gba;
        }
    }
    if (pad) {
        static const struct { SDL_GameControllerButton b; int gba; } BUTTONS[] = {
            { SDL_CONTROLLER_BUTTON_A, GBA_KEY_A }, { SDL_CONTROLLER_BUTTON_B, GBA_KEY_B },
            { SDL_CONTROLLER_BUTTON_BACK, GBA_KEY_SELECT }, { SDL_CONTROLLER_BUTTON_START, GBA_KEY_START },
            { SDL_CONTROLLER_BUTTON_DPAD_RIGHT, GBA_KEY_RIGHT }, { SDL_CONTROLLER_BUTTON_DPAD_LEFT, GBA_KEY_LEFT },
            { SDL_CONTROLLER_BUTTON_DPAD_UP, GBA_KEY_UP }, { SDL_CONTROLLER_BUTTON_DPAD_DOWN, GBA_KEY_DOWN },
            { SDL_CONTROLLER_BUTTON_RIGHTSHOULDER, GBA_KEY_R }, { SDL_CONTROLLER_BUTTON_LEFTSHOULDER, GBA_KEY_L },
        };
        for (size_t i = 0; i < sizeof(BUTTONS) / sizeof(BUTTONS[0]); ++i) {
            if (SDL_GameControllerGetButton(pad, BUTTONS[i].b)) {
                keys |= 1u << BUTTONS[i].gba;
            }
        }
        const int dead = 16000;
        int ax = SDL_GameControllerGetAxis(pad, SDL_CONTROLLER_AXIS_LEFTX);
        int ay = SDL_GameControllerGetAxis(pad, SDL_CONTROLLER_AXIS_LEFTY);
        if (ax > dead) keys |= 1u << GBA_KEY_RIGHT;
        if (ax < -dead) keys |= 1u << GBA_KEY_LEFT;
        if (ay > dead) keys |= 1u << GBA_KEY_DOWN;
        if (ay < -dead) keys |= 1u << GBA_KEY_UP;
    }
    return keys;
}

static int load_script(struct mScriptEngineContext* lua, const char* path) {
    struct VFile* vf = VFileOpen(path, O_RDONLY);
    if (!vf) {
        fprintf(stderr, "could not open script %s\n", path);
        return 0;
    }
    int ok = lua->load(lua, path, vf) && lua->run(lua);
    if (!ok) {
        fprintf(stderr, "script error: %s\n", lua->getError(lua));
    }
    vf->close(vf);
    return ok;
}

int main(int argc, char** argv) {
    const char* rom = NULL;
    const char* script = NULL;
    int scale = 3;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--scale") && i + 1 < argc) {
            scale = atoi(argv[++i]);
        } else if (!rom) {
            rom = argv[i];
        } else if (!script) {
            script = argv[i];
        }
    }
    if (!rom) {
        fprintf(stderr, "usage: %s ROM [SCRIPT] [--scale N]\n", argv[0]);
        return 2;
    }
    long exit_after = getenv("SHADOW_PLAY_EXIT_AFTER") ? strtol(getenv("SHADOW_PLAY_EXIT_AFTER"), NULL, 10) : 0;

    struct mLogger logger = { .log = log_cb };
    mLogSetDefaultLogger(&logger);

    struct mCore* core = mCoreFind(rom);
    if (!core || !core->init(core)) {
        fprintf(stderr, "could not create a core for %s\n", rom);
        return 1;
    }
    mCoreInitConfig(core, "shadow-play");
    unsigned width, height;
    core->desiredVideoDimensions(core, &width, &height);
    color_t* pixels = calloc((size_t) width * height, sizeof(color_t));
    core->setVideoBuffer(core, pixels, width);
    core->setAudioBufferSize(core, 1024);
    if (!mCoreLoadFile(core, rom)) {
        fprintf(stderr, "could not load %s\n", rom);
        return 1;
    }
    mCoreAutoloadSave(core);
    core->reset(core);

    struct mScriptContext context;
    mScriptContextInit(&context);
    mScriptContextAttachStdlib(&context);
    mScriptContextAttachSocket(&context);
    mScriptContextAttachCore(&context, core);
    mScriptContextAttachLogger(&context, &logger);
    struct mScriptEngineContext* lua = mScriptContextRegisterEngine(&context, mSCRIPT_ENGINE_LUA);
    struct mCoreCallbacks callbacks = { .context = &context, .videoFrameEnded = frame_ended };
    core->addCoreCallbacks(core, &callbacks);
    if (script && !load_script(lua, script)) {
        return 1;
    }
    mScriptContextTriggerCallback(&context, "start");

    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_AUDIO | SDL_INIT_GAMECONTROLLER) != 0) {
        fprintf(stderr, "SDL_Init: %s\n", SDL_GetError());
        return 1;
    }
    char title[64] = { 0 };
    core->getGameTitle(core, title);
    char caption[128];
    snprintf(caption, sizeof(caption), "shadow-play - %s", title[0] ? title : rom);
    SDL_Window* window = SDL_CreateWindow(caption, SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
                                          width * scale, height * scale, SDL_WINDOW_RESIZABLE);
    SDL_Renderer* renderer = SDL_CreateRenderer(window, -1, SDL_RENDERER_ACCELERATED);
    if (!renderer) {
        renderer = SDL_CreateRenderer(window, -1, 0);
    }
    SDL_RenderSetLogicalSize(renderer, width, height);
    // color_t is 32-bit XBGR: bytes R, G, B, X in memory.
    SDL_Texture* texture = SDL_CreateTexture(renderer, SDL_PIXELFORMAT_ABGR8888, SDL_TEXTUREACCESS_STREAMING, width, height);

    SDL_AudioSpec want = { .freq = AUDIO_RATE, .format = AUDIO_S16SYS, .channels = 2, .samples = 1024 }, have;
    SDL_AudioDeviceID audio = SDL_OpenAudioDevice(NULL, 0, &want, &have, 0);
    struct blip_t* left = core->getAudioChannel(core, 0);
    struct blip_t* right = core->getAudioChannel(core, 1);
    if (audio) {
        blip_set_rates(left, core->frequency(core), AUDIO_RATE);
        blip_set_rates(right, core->frequency(core), AUDIO_RATE);
        SDL_PauseAudioDevice(audio, 0);
    }
    static short samples[4096 * 2];

    SDL_GameController* pad = NULL;
    for (int i = 0; i < SDL_NumJoysticks(); ++i) {
        if (SDL_IsGameController(i) && (pad = SDL_GameControllerOpen(i))) {
            break;
        }
    }

    const double frame_seconds = (double) core->frameCycles(core) / core->frequency(core);
    const Uint64 freq = SDL_GetPerformanceFrequency();
    Uint64 next = SDL_GetPerformanceCounter();
    long frames = 0;
    int running = 1;
    while (running) {
        SDL_Event ev;
        while (SDL_PollEvent(&ev)) {
            if (ev.type == SDL_QUIT) {
                running = 0;
            } else if (ev.type == SDL_KEYDOWN && !ev.key.repeat) {
                if (ev.key.keysym.sym == SDLK_ESCAPE) {
                    running = 0;
                } else if (ev.key.keysym.sym == SDLK_F5) {
                    fprintf(stderr, "save state: %s\n", mCoreSaveState(core, 1, SAVESTATE_SCREENSHOT | SAVESTATE_RTC) ? "ok" : "failed");
                } else if (ev.key.keysym.sym == SDLK_F8) {
                    fprintf(stderr, "load state: %s\n", mCoreLoadState(core, 1, SAVESTATE_RTC) ? "ok" : "failed");
                }
            } else if (ev.type == SDL_CONTROLLERDEVICEADDED && !pad) {
                pad = SDL_GameControllerOpen(ev.cdevice.which);
            }
        }
        const Uint8* kb = SDL_GetKeyboardState(NULL);
        int fast = kb[SDL_SCANCODE_TAB];

        core->setKeys(core, read_keys(pad));
        core->runFrame(core);
        ++frames;

        int avail = blip_samples_avail(left);
        if (avail > 4096) avail = 4096;
        if (avail > 0) {
            blip_read_samples(left, samples, avail, 1);
            blip_read_samples(right, samples + 1, avail, 1);
            if (audio && !fast && SDL_GetQueuedAudioSize(audio) < AUDIO_RATE * 4 * AUDIO_MAX_QUEUED_MS / 1000) {
                SDL_QueueAudio(audio, samples, avail * 4);
            }
        }

        SDL_UpdateTexture(texture, NULL, pixels, width * sizeof(color_t));
        SDL_RenderClear(renderer);
        SDL_RenderCopy(renderer, texture, NULL, NULL);
        SDL_RenderPresent(renderer);

        if (exit_after && frames >= exit_after) {
            running = 0;
        }
        if (fast) {
            next = SDL_GetPerformanceCounter();
            continue;
        }
        next += (Uint64) (frame_seconds * freq);
        Uint64 now = SDL_GetPerformanceCounter();
        if (now < next) {
            Uint32 ms = (Uint32) ((next - now) * 1000 / freq);
            if (ms > 1) SDL_Delay(ms - 1);
            while (SDL_GetPerformanceCounter() < next) {}
        } else if (now - next > freq / 10) {
            next = now;  // fell far behind (e.g. window dragged): don't try to catch up
        }
    }

    mScriptContextTriggerCallback(&context, "shutdown");
    mScriptContextDeinit(&context);
    if (audio) SDL_CloseAudioDevice(audio);
    if (pad) SDL_GameControllerClose(pad);
    SDL_DestroyTexture(texture);
    SDL_DestroyRenderer(renderer);
    SDL_DestroyWindow(window);
    SDL_Quit();
    mCoreConfigDeinit(&core->config);
    core->deinit(core);   // flushes battery saves
    free(pixels);
    return 0;
}
