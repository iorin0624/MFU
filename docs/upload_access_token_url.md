# Upload access-token URL rollout

The generated URL now uses `/view/<uuid>/access?token=<token>` so that messaging
apps do not interpret `#` as a hashtag. The bearer token and its stored hash
are unchanged. Previously issued `/view/<uuid>/access#<token>` links must keep
working.

Before enabling this on the public site, inspect every HTTP access logger in
front of the Flask app (including Apache and Gunicorn). A log format containing
the full request line or query string, such as Apache `%r` or `%q`, records the
bearer token. Configure those loggers to omit query strings for the
`/view/<uuid>/access` endpoint, or exclude that endpoint from access logging.
Do not rely on Flask's own path-only access log for this check.

The MFU Apache virtual hosts currently use `combined` access logs. In each
port-80, IPv4 TLS, and IPv6 TLS virtual host, add this rule and use the same
environment flag in its `CustomLog ... env=!mfu_skip_access_log` directive:

```apache
SetEnvIf Request_URI "^/view/[^/]+/access([?].*)?$" mfu_skip_access_log
```

Keep the existing deleted-album log exclusion by changing its `SetEnvIf`
environment flag to `mfu_skip_access_log` as well. Validate with
`apache2ctl -t` and reload Apache before publishing new-format links.

The access page sets `Referrer-Policy: no-referrer`, disallows caching, removes
the token from browser history before verification, and submits it by POST.
The application access-log Referer field also strips query strings from upload
access URLs as defense in depth. Link-preview GET requests do not consume or
rotate the token.

Verify after deployment with a newly generated URL and an existing `#` URL:
both should open the protected view, and neither the app nor the proxy access
logs should contain the token. Use a disposable test upload for log inspection;
never copy a production token into diagnostics.
