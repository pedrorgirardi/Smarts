# AGENTS

## Smarts Companion (SwiftUI)

This repo includes a native macOS helper app named `companion` used for UI
features that Sublime Text does not provide (currently toast notifications).

### Location
- Source: `companion/`
- Swift package: `companion/Package.swift`
- Entry point: `companion/Sources/companion/main.swift`
- Installed binary (local): `bin/companion`

### Build (local)
```
cd companion
swift build -c release
cp .build/release/companion ../bin/companion
chmod +x ../bin/companion
```

### Integration
Smarts calls `bin/companion` from `lib/smarts_client.py` when an LSP server
initializes, using the `toast` subcommand.
