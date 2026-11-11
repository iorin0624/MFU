import Foundation
import UniformTypeIdentifiers

enum URLExtractorError: LocalizedError {
    case noInput
    case invalidURL

    var errorDescription: String? {
        switch self {
        case .noInput:
            return "共有データからURLを取得できませんでした。"
        case .invalidURL:
            return "URL形式が不正です。"
        }
    }
}

enum URLExtractor {
    static func extractFirstURL(from extensionItems: [NSExtensionItem]?) async throws -> URL {
        guard let extensionItems, !extensionItems.isEmpty else {
            throw URLExtractorError.noInput
        }

        for item in extensionItems {
            guard let attachments = item.attachments else { continue }

            for provider in attachments {
                if provider.hasItemConformingToTypeIdentifier(UTType.url.identifier) {
                    if let url = try await loadURL(from: provider) {
                        return url
                    }
                }

                if provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier) {
                    if let url = try await loadURLFromPlainText(from: provider) {
                        return url
                    }
                }
            }
        }

        throw URLExtractorError.noInput
    }

    private static func loadURL(from provider: NSItemProvider) async throws -> URL? {
        let item = try await provider.loadItem(forTypeIdentifier: UTType.url.identifier)

        if let url = item as? URL {
            return url
        }

        if let str = item as? String {
            return normalizeURL(from: str)
        }

        return nil
    }

    private static func loadURLFromPlainText(from provider: NSItemProvider) async throws -> URL? {
        let item = try await provider.loadItem(forTypeIdentifier: UTType.plainText.identifier)

        if let str = item as? String {
            return normalizeURL(from: str)
        }

        if let url = item as? URL {
            return url
        }

        return nil
    }

    private static func normalizeURL(from raw: String) -> URL? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        return URL(string: trimmed)
    }
}

private extension NSItemProvider {
    func loadItem(forTypeIdentifier typeIdentifier: String) async throws -> NSSecureCoding? {
        try await withCheckedThrowingContinuation { continuation in
            loadItem(forTypeIdentifier: typeIdentifier, options: nil) { item, error in
                if let error {
                    continuation.resume(throwing: error)
                    return
                }
                continuation.resume(returning: item)
            }
        }
    }
}
