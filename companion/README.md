# companion

A small SwiftUI helper for Smarts. Currently supports a `toast` command for
showing a bottom-right snackbar-style notification.

## Icon assets

The official companion icon files are:

- `assets/companion-icon-1024.png`
- `assets/companion.icns`

To update both files from a single source icon (for example, Sublime Text's
app icon), run:

```sh
./scripts/update_icon.sh "/Applications/Sublime Text.app/Contents/Resources/Sublime Text.icns"
```

## Build

```sh
swift build -c release
```

Copy the resulting binary to `bin/companion`:

```sh
cp .build/release/companion ../bin/companion
chmod +x ../bin/companion
```

## Usage

```sh
bin/companion toast --message "Server Pyright initialized" --duration 2
```
