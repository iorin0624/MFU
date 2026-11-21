import Foundation

enum DownloadError: LocalizedError {
    case invalidResponse
    case unauthorizedOrExpired
    case htmlPageURL
    case notJpegContentType(String?)
    case invalidJpegBinary
    case unsupportedStatus(Int)

    var errorDescription: String? {
        switch self {
        case .invalidResponse:
            return "レスポンスの取得に失敗しました。"
        case .unauthorizedOrExpired:
            return "期限切れ/権限なしのURLです。"
        case .htmlPageURL:
            return "閲覧ページURLです。保存用URLが必要です。"
        case .notJpegContentType(let contentType):
            return "JPEGではありません（Content-Type: \(contentType ?? "不明")）。"
        case .invalidJpegBinary:
            return "JPEGデータとして不正です。"
        case .unsupportedStatus(let status):
            return "保存できませんでした（HTTP \(status)）。"
        }
    }
}

struct DownloadResult {
    let tempFileURL: URL
}

enum JpegDownloader {
    static func downloadJPEG(from url: URL, session: URLSession = .shared) async throws -> DownloadResult {
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 20

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw DownloadError.invalidResponse
        }

        switch http.statusCode {
        case 200:
            break
        case 401, 403, 410:
            throw DownloadError.unauthorizedOrExpired
        default:
            throw DownloadError.unsupportedStatus(http.statusCode)
        }

        let contentType = http.value(forHTTPHeaderField: "Content-Type")?.lowercased()
        if let contentType, contentType.contains("text/html") {
            throw DownloadError.htmlPageURL
        }

        guard isJpegContentType(contentType) else {
            throw DownloadError.notJpegContentType(contentType)
        }

        guard startsWithJpegSOI(data) else {
            throw DownloadError.invalidJpegBinary
        }

        let tempURL = try writeToTempFile(data)
        return DownloadResult(tempFileURL: tempURL)
    }

    private static func isJpegContentType(_ contentType: String?) -> Bool {
        guard let contentType else { return false }
        return contentType.contains("image/jpeg") || contentType.contains("image/jpg")
    }

    private static func startsWithJpegSOI(_ data: Data) -> Bool {
        guard data.count >= 2 else { return false }
        return data[0] == 0xFF && data[1] == 0xD8
    }

    private static func writeToTempFile(_ data: Data) throws -> URL {
        let fileName = "share_\(UUID().uuidString).jpg"
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(fileName)
        try data.write(to: url, options: .atomic)
        return url
    }
}
