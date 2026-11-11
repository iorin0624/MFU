import UIKit

final class ShareViewController: UIViewController {
    private enum ProgressState {
        case idle
        case downloading
        case saving
    }

    private let urlLabel = UILabel()
    private let messageLabel = UILabel()
    private let progressLabel = UILabel()
    private let saveButton = UIButton(type: .system)
    private let closeButton = UIButton(type: .system)

    private var sharedURL: URL?

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        configureUI()

        Task {
            await loadSharedURL()
        }
    }

    private func configureUI() {
        let titleLabel = UILabel()
        titleLabel.text = "JPEGを写真に保存"
        titleLabel.font = .preferredFont(forTextStyle: .headline)

        urlLabel.numberOfLines = 2
        urlLabel.font = .preferredFont(forTextStyle: .subheadline)
        urlLabel.textColor = .secondaryLabel

        progressLabel.font = .preferredFont(forTextStyle: .footnote)
        progressLabel.textColor = .secondaryLabel
        progressLabel.text = "待機中"

        messageLabel.numberOfLines = 0
        messageLabel.font = .preferredFont(forTextStyle: .body)

        saveButton.setTitle("写真に保存", for: .normal)
        saveButton.addTarget(self, action: #selector(saveTapped), for: .touchUpInside)
        saveButton.isEnabled = false

        closeButton.setTitle("閉じる", for: .normal)
        closeButton.addTarget(self, action: #selector(closeTapped), for: .touchUpInside)

        let stack = UIStackView(arrangedSubviews: [titleLabel, urlLabel, saveButton, progressLabel, messageLabel, closeButton])
        stack.axis = .vertical
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false

        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16),
            stack.centerYAnchor.constraint(equalTo: view.centerYAnchor)
        ])
    }

    @MainActor
    private func loadSharedURL() async {
        do {
            let url = try await URLExtractor.extractFirstURL(from: extensionContext?.inputItems as? [NSExtensionItem])
            sharedURL = url
            urlLabel.text = "URL: \(shorten(url.absoluteString))"
            messageLabel.text = ""
            saveButton.isEnabled = true
        } catch {
            urlLabel.text = "URL: 取得失敗"
            messageLabel.text = error.localizedDescription
            saveButton.isEnabled = false
        }
    }

    @objc
    private func saveTapped() {
        guard let url = sharedURL else { return }

        saveButton.isEnabled = false
        messageLabel.text = ""

        Task {
            do {
                await setProgress(.downloading)
                let result = try await JpegDownloader.downloadJPEG(from: url)
                defer { try? FileManager.default.removeItem(at: result.tempFileURL) }

                await setProgress(.saving)
                try await PhotoSaver.saveImage(at: result.tempFileURL)

                await MainActor.run {
                    progressLabel.text = "完了"
                    messageLabel.textColor = .systemGreen
                    messageLabel.text = "写真に保存しました。"
                }
            } catch {
                await MainActor.run {
                    progressLabel.text = "失敗"
                    messageLabel.textColor = .systemRed
                    messageLabel.text = error.localizedDescription
                }
            }

            await MainActor.run {
                saveButton.isEnabled = true
            }
        }
    }

    @MainActor
    private func setProgress(_ state: ProgressState) {
        switch state {
        case .idle:
            progressLabel.text = "待機中"
        case .downloading:
            progressLabel.text = "ダウンロード中…"
        case .saving:
            progressLabel.text = "保存中…"
        }
    }

    @objc
    private func closeTapped() {
        extensionContext?.completeRequest(returningItems: nil)
    }

    private func shorten(_ text: String, maxLength: Int = 80) -> String {
        guard text.count > maxLength else { return text }
        let head = text.prefix(40)
        let tail = text.suffix(20)
        return "\(head)…\(tail)"
    }
}
