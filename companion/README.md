# companion

A small SwiftUI helper for Smarts. Currently supports a `toast` command for
showing a bottom-right snackbar-style notification.

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
