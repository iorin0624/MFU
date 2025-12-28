
# --- Add the following lines into your existing __init__.py (near where `app` is defined) ---
# from mfu_zip_stream import install as install_zip_stream
# install_zip_stream(app)
#
# Optional config (defaults shown):
# app.config["STORAGE_ROOT"] = "/mnt/mfu/storage"
# app.config["ZIP_MAX_FILES"] = 5000
# app.config["ZIP_MAX_TOTAL_BYTES"] = 2 * 1024**3
# app.config["ZIP_STREAM_CONCURRENCY"] = 2
