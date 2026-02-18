import Foundation
import Photos

enum PhotoSaveError: LocalizedError {
    case denied

    var errorDescription: String? {
        switch self {
        case .denied:
            return "写真への追加権限がありません。設定アプリで「写真」権限を許可してください。"
        }
    }
}

enum PhotoSaver {
    static func saveImage(at fileURL: URL) async throws {
        let status = await PHPhotoLibrary.requestAuthorization(for: .addOnly)
        guard status == .authorized || status == .limited else {
            throw PhotoSaveError.denied
        }

        try await withCheckedThrowingContinuation { continuation in
            PHPhotoLibrary.shared().performChanges({
                PHAssetCreationRequest.forAsset().addResource(with: .photo, fileURL: fileURL, options: nil)
            }) { success, error in
                if let error {
                    continuation.resume(throwing: error)
                    return
                }

                if success {
                    continuation.resume(returning: ())
                } else {
                    continuation.resume(throwing: NSError(domain: "PhotoSaver", code: -1))
                }
            }
        }
    }
}
