# SwitchSuite

English | [简体中文](README.md)

Dual-backend account manager for ZCode and WorkBuddy (Tauri 2 desktop app). Switch tabs in the title bar:

- **ZCode tab**: one-click account switching, quota display, claim automation, multi-instance, encrypted export/import, tray / autostart / CLI
- **WorkBuddy tab**: one-click account switching (credential-level, usually no re-login), per-account session archiving with auto-restore, QR sign-in, daily check-in, credits, an OpenAI-compatible gateway, session copying (true sharing), backups & rollback

See the [Chinese README](README.md) for full documentation.

## Download

Grab `SwitchSuite_*_x64-setup.exe` from [Releases](https://github.com/Danjack85/SwitchSuite/releases).

## Build

```bat
npm install
npx tauri build
```

The WorkBuddy engine sidecar is committed at `src-tauri/sidecar/`; to rebuild it from source see [workbuddy-switch](https://github.com/Danjack85/workbuddy-switch).

## License

MIT. ZCode backend based on [pjpv/zcode-switch](https://github.com/pjpv/zcode-switch) (MIT).
