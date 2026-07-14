import 'package:flutter_test/flutter_test.dart';
import 'package:mfudownload/main.dart';

void main() {
  testWidgets('shows the waiting state', (tester) async {
    await tester.pumpWidget(const MfuDownloadApp());
    await tester.pump();

    expect(find.text('MFU Download'), findsOneWidget);
    expect(find.text('写真を選択してください'), findsOneWidget);
  });

  test('parses a download session', () {
    final session = DownloadSession.fromExchange({
      'access_token': 'mfu_dl_test',
      'manifest': {
        'job_id': 12,
        'title': '撮影データ',
        'album': 'iori0624',
        'expires_at': '2026-07-12T00:00:00Z',
        'files': [
          {
            'id': 'image-id',
            'name': 'sample.jpg',
            'size': 123,
            'download_url': 'https://mfu.iori0624.jp/file',
          },
        ],
      },
    });

    expect(session.jobId, 12);
    expect(session.files.single.name, 'sample.jpg');
    expect(session.album, 'iori0624');
  });
}
