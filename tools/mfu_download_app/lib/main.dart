import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:app_links/app_links.dart';
import 'package:device_info_plus/device_info_plus.dart';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:permission_handler/permission_handler.dart';
import 'package:saver_gallery/saver_gallery.dart';
import 'package:shared_preferences/shared_preferences.dart';

const String defaultBaseUrl = 'https://mfu.iori0624.jp';
const String albumName = 'iori0624';
const String activeSessionKey = 'active_download_session';
const String savedImageIdsKey = 'saved_mfu_image_ids';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const MfuDownloadApp());
}

class MfuDownloadApp extends StatelessWidget {
  const MfuDownloadApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'MFU Download',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xff1267d6),
          brightness: Brightness.light,
        ),
        scaffoldBackgroundColor: const Color(0xfff5f7fa),
        appBarTheme: const AppBarTheme(
          backgroundColor: Color(0xff1267d6),
          foregroundColor: Colors.white,
          centerTitle: false,
        ),
        filledButtonTheme: FilledButtonThemeData(
          style: FilledButton.styleFrom(
            minimumSize: const Size.fromHeight(52),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(7),
            ),
          ),
        ),
        cardTheme: const CardThemeData(
          margin: EdgeInsets.zero,
          elevation: 0,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.all(Radius.circular(7)),
            side: BorderSide(color: Color(0xffdce2e9)),
          ),
        ),
        useMaterial3: true,
      ),
      home: const DownloadHomePage(),
    );
  }
}

enum DownloadStage { idle, loading, ready, downloading, done, error }

class DownloadItem {
  const DownloadItem({
    required this.id,
    required this.name,
    required this.size,
    required this.downloadUrl,
  });

  factory DownloadItem.fromJson(Map<String, dynamic> json) {
    return DownloadItem(
      id: json['id'] as String,
      name: json['name'] as String,
      size: (json['size'] as num?)?.toInt() ?? 0,
      downloadUrl: json['download_url'] as String,
    );
  }

  final String id;
  final String name;
  final int size;
  final String downloadUrl;

  Map<String, dynamic> toJson() => {
    'id': id,
    'name': name,
    'size': size,
    'download_url': downloadUrl,
  };
}

class DownloadSession {
  const DownloadSession({
    required this.jobId,
    required this.accessToken,
    required this.title,
    required this.album,
    required this.expiresAt,
    required this.files,
  });

  factory DownloadSession.fromExchange(Map<String, dynamic> json) {
    final manifest = json['manifest'] as Map<String, dynamic>;
    return DownloadSession(
      jobId: (manifest['job_id'] as num).toInt(),
      accessToken: json['access_token'] as String,
      title: manifest['title'] as String? ?? 'MFU Photos',
      album: manifest['album'] as String? ?? albumName,
      expiresAt: manifest['expires_at'] as String? ?? '',
      files: (manifest['files'] as List<dynamic>? ?? const [])
          .map((item) => DownloadItem.fromJson(item as Map<String, dynamic>))
          .toList(growable: false),
    );
  }

  factory DownloadSession.fromJson(Map<String, dynamic> json) {
    return DownloadSession(
      jobId: (json['job_id'] as num).toInt(),
      accessToken: json['access_token'] as String,
      title: json['title'] as String,
      album: json['album'] as String,
      expiresAt: json['expires_at'] as String,
      files: (json['files'] as List<dynamic>)
          .map((item) => DownloadItem.fromJson(item as Map<String, dynamic>))
          .toList(growable: false),
    );
  }

  final int jobId;
  final String accessToken;
  final String title;
  final String album;
  final String expiresAt;
  final List<DownloadItem> files;

  Map<String, dynamic> toJson() => {
    'job_id': jobId,
    'access_token': accessToken,
    'title': title,
    'album': album,
    'expires_at': expiresAt,
    'files': files.map((item) => item.toJson()).toList(growable: false),
  };
}

class DownloadHomePage extends StatefulWidget {
  const DownloadHomePage({super.key});

  @override
  State<DownloadHomePage> createState() => _DownloadHomePageState();
}

class _DownloadHomePageState extends State<DownloadHomePage> {
  final AppLinks _appLinks = AppLinks();
  final Set<String> _handledLinks = <String>{};
  StreamSubscription<Uri>? _linkSubscription;
  DownloadStage _stage = DownloadStage.idle;
  DownloadSession? _session;
  String _message = '';
  String _currentName = '';
  int _processed = 0;
  int _saved = 0;
  int _skipped = 0;
  int _failed = 0;
  bool _forceResave = false;
  bool _cancelRequested = false;

  @override
  void initState() {
    super.initState();
    _linkSubscription = _appLinks.uriLinkStream.listen(
      (uri) => unawaited(_handleLink(uri)),
      onError: (Object error) => _showError('アプリへのリンクを開けませんでした。'),
    );
    unawaited(_initialize());
  }

  Future<void> _initialize() async {
    await _restoreSession();
    final initialLink = await _appLinks.getInitialLink();
    if (initialLink != null) {
      await _handleLink(initialLink);
    }
  }

  @override
  void dispose() {
    _linkSubscription?.cancel();
    super.dispose();
  }

  Future<void> _restoreSession() async {
    final preferences = await SharedPreferences.getInstance();
    final raw = preferences.getString(activeSessionKey);
    if (raw == null || raw.isEmpty) {
      return;
    }
    try {
      final session = DownloadSession.fromJson(
        jsonDecode(raw) as Map<String, dynamic>,
      );
      if (!mounted) return;
      setState(() {
        _session = session;
        _stage = DownloadStage.ready;
        _message = '前回のダウンロードを再開できます。';
      });
    } catch (_) {
      await preferences.remove(activeSessionKey);
    }
  }

  String? _launchToken(Uri uri) {
    if (uri.scheme == 'mfudownload' && uri.host == 'job') {
      return uri.queryParameters['token'];
    }
    final segments = uri.pathSegments;
    if (uri.scheme == 'https' &&
        segments.length == 3 &&
        segments[0] == 'mobile-download' &&
        segments[1] == 'open') {
      return segments[2];
    }
    return null;
  }

  String _baseUrl(Uri uri) {
    if (uri.scheme == 'https' && uri.host.isNotEmpty) {
      return '${uri.scheme}://${uri.authority}';
    }
    return defaultBaseUrl;
  }

  Future<void> _handleLink(Uri uri) async {
    final rawLink = uri.toString();
    if (!_handledLinks.add(rawLink)) return;
    final launchToken = _launchToken(uri);
    if (launchToken == null || !launchToken.startsWith('mfu_launch_')) {
      return;
    }

    if (mounted) {
      setState(() {
        _stage = DownloadStage.loading;
        _message = '選択した写真を確認しています…';
      });
    }

    try {
      final response = await http
          .post(
            Uri.parse('${_baseUrl(uri)}/mobile-download/api/exchange'),
            headers: const {'Content-Type': 'application/json'},
            body: jsonEncode({
              'launch_token': launchToken,
              'platform': Platform.isIOS ? 'ios' : 'android',
            }),
          )
          .timeout(const Duration(seconds: 30));
      final data = jsonDecode(response.body) as Map<String, dynamic>;
      if (response.statusCode != 200 || data['ok'] != true) {
        throw StateError(data['error'] as String? ?? 'exchange_failed');
      }
      final session = DownloadSession.fromExchange(data);
      final preferences = await SharedPreferences.getInstance();
      await preferences.setString(
        activeSessionKey,
        jsonEncode(session.toJson()),
      );
      if (!mounted) return;
      setState(() {
        _session = session;
        _stage = DownloadStage.ready;
        _message = '${session.files.length}枚のJPEGを保存します。';
        _processed = 0;
        _saved = 0;
        _skipped = 0;
        _failed = 0;
      });
    } on TimeoutException {
      _showError('サーバーへの接続がタイムアウトしました。');
    } catch (error) {
      final text = error.toString();
      if (text.contains('launch_token_used')) {
        _showError('このリンクは使用済みです。Webからもう一度開いてください。');
      } else if (text.contains('launch_token_expired')) {
        _showError('リンクの有効期限が切れました。Webからもう一度開いてください。');
      } else {
        _showError('写真情報を取得できませんでした。');
      }
    }
  }

  void _showError(String message) {
    if (!mounted) return;
    setState(() {
      _stage = DownloadStage.error;
      _message = message;
    });
  }

  Future<bool> _requestPhotoPermission() async {
    if (Platform.isIOS) {
      final status = await Permission.photos.request();
      return status.isGranted || status.isLimited;
    }
    if (Platform.isAndroid) {
      final sdk = (await DeviceInfoPlugin().androidInfo).version.sdkInt;
      if (sdk < 29) {
        return (await Permission.storage.request()).isGranted;
      }
      return true;
    }
    return false;
  }

  Future<Set<String>> _savedImageIds() async {
    final preferences = await SharedPreferences.getInstance();
    return (preferences.getStringList(savedImageIdsKey) ?? const <String>[])
        .toSet();
  }

  Future<void> _storeSavedImageIds(Set<String> ids) async {
    final preferences = await SharedPreferences.getInstance();
    final values = ids.toList(growable: false);
    final start = values.length > 10000 ? values.length - 10000 : 0;
    await preferences.setStringList(savedImageIdsKey, values.sublist(start));
  }

  Future<void> _startDownload() async {
    final session = _session;
    if (session == null || _stage == DownloadStage.downloading) return;
    if (!await _requestPhotoPermission()) {
      _showError('写真へのアクセスが許可されていません。端末の設定から許可してください。');
      return;
    }

    final savedIds = await _savedImageIds();
    _cancelRequested = false;
    setState(() {
      _stage = DownloadStage.downloading;
      _message = '「$albumName」アルバムへ保存しています…';
      _processed = 0;
      _saved = 0;
      _skipped = 0;
      _failed = 0;
      _currentName = '';
    });

    for (final item in session.files) {
      if (_cancelRequested) break;
      if (!_forceResave && savedIds.contains(item.id)) {
        if (!mounted) return;
        setState(() {
          _processed += 1;
          _skipped += 1;
          _currentName = item.name;
        });
        continue;
      }

      if (mounted) {
        setState(() => _currentName = item.name);
      }
      try {
        final response = await http
            .get(
              Uri.parse(item.downloadUrl),
              headers: {'Authorization': 'Bearer ${session.accessToken}'},
            )
            .timeout(const Duration(minutes: 3));
        if (response.statusCode != 200) {
          throw HttpException('download_${response.statusCode}');
        }
        if (item.size > 0 && response.bodyBytes.length != item.size) {
          throw const FormatException('file_size_mismatch');
        }
        final result = await SaverGallery.saveImage(
          response.bodyBytes,
          fileName: item.name,
          albumPath: albumName,
          skipIfExists: false,
        );
        if (!result.isSuccess) {
          throw StateError(result.errorMessage ?? 'photo_save_failed');
        }
        savedIds.add(item.id);
        await _storeSavedImageIds(savedIds);
        if (!mounted) return;
        setState(() {
          _processed += 1;
          _saved += 1;
        });
      } catch (_) {
        if (!mounted) return;
        setState(() {
          _processed += 1;
          _failed += 1;
        });
      }
    }

    if (!mounted) return;
    if (_cancelRequested) {
      setState(() {
        _stage = DownloadStage.ready;
        _message = '保存を中止しました。続きから再開できます。';
      });
      return;
    }
    if (_failed > 0) {
      setState(() {
        _stage = DownloadStage.error;
        _message = '$_failed枚を保存できませんでした。再試行してください。';
      });
      return;
    }

    await _completeSession(session);
    if (!mounted) return;
    setState(() {
      _stage = DownloadStage.done;
      _message = '「$albumName」アルバムへ保存しました。';
      _currentName = '';
    });
  }

  Future<void> _completeSession(DownloadSession session) async {
    try {
      await http
          .post(
            Uri.parse(
              '$defaultBaseUrl/mobile-download/api/jobs/${session.jobId}/complete',
            ),
            headers: {'Authorization': 'Bearer ${session.accessToken}'},
          )
          .timeout(const Duration(seconds: 20));
    } catch (_) {
      // Photos are already safely stored. Server cleanup also expires the job.
    }
    final preferences = await SharedPreferences.getInstance();
    await preferences.remove(activeSessionKey);
  }

  void _cancelDownload() {
    _cancelRequested = true;
    setState(() => _message = '現在の写真が終わり次第停止します…');
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('MFU Download')),
      body: SafeArea(
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 560),
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: AnimatedSwitcher(
                duration: const Duration(milliseconds: 180),
                child: _buildStage(),
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildStage() {
    switch (_stage) {
      case DownloadStage.idle:
        return _messagePanel(
          key: const ValueKey('idle'),
          icon: Icons.photo_library_outlined,
          title: '写真を選択してください',
          message: 'MFUのWeb画面から、保存する写真を選択してください。',
        );
      case DownloadStage.loading:
        return _messagePanel(
          key: const ValueKey('loading'),
          icon: Icons.downloading,
          title: '準備中',
          message: _message,
          progress: const CircularProgressIndicator(),
        );
      case DownloadStage.ready:
        return _readyPanel();
      case DownloadStage.downloading:
        return _progressPanel();
      case DownloadStage.done:
        return _messagePanel(
          key: const ValueKey('done'),
          icon: Icons.check_circle,
          iconColor: const Color(0xff16804b),
          title: '保存完了',
          message: '$_message\n保存 $_saved枚・スキップ $_skipped枚',
        );
      case DownloadStage.error:
        return _messagePanel(
          key: const ValueKey('error'),
          icon: Icons.error_outline,
          iconColor: const Color(0xffb42318),
          title: '確認してください',
          message: _message,
          action: _session == null
              ? null
              : FilledButton.icon(
                  onPressed: _startDownload,
                  icon: const Icon(Icons.refresh),
                  label: const Text('再試行'),
                ),
        );
    }
  }

  Widget _readyPanel() {
    final session = _session!;
    return ListView(
      key: const ValueKey('ready'),
      shrinkWrap: true,
      children: [
        Text(session.title, style: Theme.of(context).textTheme.headlineSmall),
        const SizedBox(height: 6),
        Text(
          '${session.files.length}枚のJPEG',
          style: Theme.of(context).textTheme.bodyLarge,
        ),
        const SizedBox(height: 20),
        Card(
          child: ListTile(
            leading: const Icon(Icons.photo_album_outlined),
            title: const Text('保存先'),
            subtitle: Text(session.album),
          ),
        ),
        const SizedBox(height: 12),
        SwitchListTile(
          contentPadding: EdgeInsets.zero,
          title: const Text('保存済みの写真も再保存'),
          value: _forceResave,
          onChanged: (value) => setState(() => _forceResave = value),
        ),
        if (_message.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(_message, style: const TextStyle(color: Color(0xff526071))),
        ],
        const SizedBox(height: 24),
        FilledButton.icon(
          onPressed: _startDownload,
          icon: const Icon(Icons.download),
          label: const Text('保存を開始'),
        ),
      ],
    );
  }

  Widget _progressPanel() {
    final total = _session?.files.length ?? 0;
    final value = total == 0 ? 0.0 : _processed / total;
    return ListView(
      key: const ValueKey('progress'),
      shrinkWrap: true,
      children: [
        Text('保存中', style: Theme.of(context).textTheme.headlineSmall),
        const SizedBox(height: 18),
        LinearProgressIndicator(value: value, minHeight: 10),
        const SizedBox(height: 10),
        Text('$_processed / $total'),
        const SizedBox(height: 18),
        Text(
          _currentName,
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
          style: Theme.of(context).textTheme.bodyLarge,
        ),
        const SizedBox(height: 8),
        Text('保存 $_saved枚・スキップ $_skipped枚・失敗 $_failed枚'),
        const SizedBox(height: 24),
        OutlinedButton.icon(
          onPressed: _cancelRequested ? null : _cancelDownload,
          icon: const Icon(Icons.stop_circle_outlined),
          label: const Text('中止'),
        ),
      ],
    );
  }

  Widget _messagePanel({
    required Key key,
    required IconData icon,
    required String title,
    required String message,
    Color iconColor = const Color(0xff1267d6),
    Widget? progress,
    Widget? action,
  }) {
    return Column(
      key: key,
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 64, color: iconColor),
        const SizedBox(height: 18),
        Text(title, style: Theme.of(context).textTheme.headlineSmall),
        const SizedBox(height: 10),
        Text(message, textAlign: TextAlign.center),
        if (progress != null) ...[const SizedBox(height: 24), progress],
        if (action != null) ...[const SizedBox(height: 24), action],
      ],
    );
  }
}
