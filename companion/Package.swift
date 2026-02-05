// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "companion",
    platforms: [
        .macOS(.v12)
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-argument-parser", from: "1.3.0")
    ],
    targets: [
        .executableTarget(
            name: "companion",
            dependencies: [
                .product(name: "ArgumentParser", package: "swift-argument-parser")
            ]
        )
    ]
)
