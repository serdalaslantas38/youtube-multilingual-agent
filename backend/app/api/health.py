"""Health and readiness HTTP endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse

from app.core.app_config import aload_app_config
from app.core.settings import Settings
from app.db.session import check_database_health
from app.schemas.health import HealthResponse, ReadinessChecks, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """Lightweight liveness check that never reveals secrets."""
    settings: Settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        service="youtube-multilingual-agent",
        version=request.app.version,
        environment=settings.app_env.value,
        youtube_publishing_enabled=settings.youtube_publishing_enabled,
    )


@router.get("/ready", response_model=ReadinessResponse)
async def readiness_check(request: Request, response: Response) -> ReadinessResponse:
    """Readiness probe verifying database connectivity and YAML configuration."""
    settings: Settings = request.app.state.settings
    engine = request.app.state.engine

    db_ok, db_detail = await check_database_health(engine)

    cfg_ok = False
    cfg_detail = "unloaded"
    auto_publish = False
    source_lang: str | None = None
    enabled_langs: list[str] = []

    try:
        app_cfg = await aload_app_config(settings.config_dir)
        cfg_ok = True
        cfg_detail = "ok"
        auto_publish = app_cfg.policy.global_.auto_publish_enabled
        source_lang = app_cfg.languages.source_language.code
        enabled_langs = app_cfg.languages.enabled_codes
    except Exception as exc:
        cfg_detail = f"{type(exc).__name__}: {exc}"

    all_ready = db_ok and cfg_ok
    if not all_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if all_ready else "degraded",
        environment=settings.app_env.value,
        youtube_publishing_enabled=settings.youtube_publishing_enabled,
        auto_publish_enabled=auto_publish,
        source_language=source_lang,
        enabled_target_languages=enabled_langs,
        checks=ReadinessChecks(
            database=db_ok,
            database_detail=db_detail,
            config=cfg_ok,
            config_detail=cfg_detail,
        ),
    )


_BRANDING_STYLE = """
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, sans-serif;
    line-height: 1.65;
    color: #0f172a;
    background: #f8fafc;
    margin: 0;
    padding: 2.5rem 1.25rem;
  }
  .container {
    max-width: 820px;
    margin: 0 auto;
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 2.25rem;
    box-shadow: 0 2px 8px rgba(15, 23, 42, 0.04);
  }
  h1 { margin-top: 0; color: #0f172a; font-size: 1.75rem; }
  h2 { color: #1e293b; font-size: 1.2rem; margin-top: 1.5rem; }
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .nav {
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 1px solid #e2e8f0;
    display: flex;
    gap: 1.25rem;
    font-weight: 600;
  }
  .badge {
    display: inline-block;
    background: #eff6ff;
    color: #1d4ed8;
    border: 1px solid #bfdbfe;
    border-radius: 999px;
    padding: 0.2rem 0.7rem;
    font-size: 0.8rem;
    font-weight: 600;
    margin-bottom: 0.75rem;
  }
"""


@router.get("/home", response_class=HTMLResponse)
async def branding_homepage() -> HTMLResponse:
    """Public Application Homepage required by Google OAuth Consent Screen Branding."""
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>YouTube Multilingual Publishing Agent — Official Application Homepage</title>
  <style>{_BRANDING_STYLE}</style>
</head>
<body>
  <div class="container">
    <span class="badge">Official Application Homepage</span>
    <h1>YouTube Multilingual Publishing Agent</h1>
    <p>
      <strong>YouTube Multilingual Publishing Agent</strong> is a creator-operated localization
      and publishing workflow application designed for the YouTube channel
      <strong>HayataDairPüfNoktalar</strong> (<code>@orchidhacks</code>).
    </p>
    <h2>What This Application Does</h2>
    <ul>
      <li>
        Discovers videos and inspects Turkish source subtitle tracks from the authenticated
        creator's YouTube channel via the official YouTube Data API v3.
      </li>
      <li>
        Translates and validates video titles, descriptions, and SRT subtitle tracks across 48
        target languages with human-in-the-loop quality review.
      </li>
      <li>
        Publishes approved localized metadata (<code>videos.update</code>) and subtitle tracks
        (<code>captions.insert</code>) back to the creator's own YouTube videos with post-write
        verification.
      </li>
    </ul>
    <h2>Data Protection &amp; Google API Compliance</h2>
    <p>
      This application accesses YouTube Data API v3 solely on behalf of the authenticated
      channel owner. OAuth 2.0 refresh tokens are encrypted at rest with Fernet
      (AES-128-CBC + HMAC-SHA256) and are never shared with third parties or exposed to
      language models.
    </p>
    <div class="nav">
      <a href="/privacy">Privacy Policy</a>
      <a href="/terms">Terms of Service</a>
      <a href="/">Open Operator Dashboard</a>
    </div>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.get("/privacy", response_class=HTMLResponse)
async def branding_privacy_policy() -> HTMLResponse:
    """Public Privacy Policy required by Google OAuth Consent Screen Branding."""
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Privacy Policy — YouTube Multilingual Publishing Agent</title>
  <style>{_BRANDING_STYLE}</style>
</head>
<body>
  <div class="container">
    <span class="badge">Privacy Policy</span>
    <h1>Privacy Policy — YouTube Multilingual Publishing Agent</h1>
    <p><strong>Effective Date:</strong> November 2026</p>
    <p>
      This Privacy Policy explains how <strong>YouTube Multilingual Publishing Agent</strong>
      accesses, uses, stores, and protects Google and YouTube user data when authorized by a
      YouTube channel owner.
    </p>
    <h2>1. Google &amp; YouTube API Services Disclosure</h2>
    <p>
      YouTube Multilingual Publishing Agent uses YouTube API Services and complies with the
      <a href="https://developers.google.com/terms/api-services-user-data-policy"
         target="_blank" rel="noopener">Google API Services User Data Policy</a>,
      including the Limited Use requirements, and the
      <a href="https://developers.google.com/youtube/terms/api-services-terms-of-service"
         target="_blank" rel="noopener">YouTube API Services Terms of Service</a>.
      Please also review the
      <a href="http://www.google.com/policies/privacy"
         target="_blank" rel="noopener">Google Privacy Policy</a>.
    </p>
    <h2>2. Data We Access</h2>
    <ul>
      <li>
        <strong>YouTube Channel &amp; Video Metadata:</strong> Channel ID, channel title,
        uploads playlist items, video titles, descriptions, localizations, and caption tracks
        via scope <code>https://www.googleapis.com/auth/youtube.force-ssl</code>.
      </li>
      <li>
        <strong>Operator Identity:</strong> Basic account email address (<code>openid</code>,
        <code>email</code>) to verify authorized dashboard operators.
      </li>
    </ul>
    <h2>3. How We Use and Store Your Data</h2>
    <ul>
      <li>
        Data is used exclusively to generate, review, validate, and publish multilingual titles,
        descriptions, and closed captions for the authorized YouTube channel.
      </li>
      <li>
        OAuth 2.0 refresh tokens are encrypted at rest using symmetric authenticated encryption
        (Fernet) and stored only in the operator's database.
      </li>
      <li>
        We never sell, rent, or transfer Google user data to third parties, and we never expose
        OAuth tokens or secrets to frontend clients or external LLMs.
      </li>
    </ul>
    <h2>4. Revoking Access &amp; Data Deletion</h2>
    <p>
      You may revoke this application's access to your Google Account at any time via the
      <a href="https://security.google.com/settings/security/permissions"
         target="_blank" rel="noopener">Google Security Settings page</a>.
      Upon revocation or request, stored OAuth credentials and cached channel records are
      deleted immediately.
    </p>
    <div class="nav">
      <a href="/home">Application Homepage</a>
      <a href="/terms">Terms of Service</a>
    </div>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.get("/terms", response_class=HTMLResponse)
async def branding_terms_of_service() -> HTMLResponse:
    """Public Terms of Service for Google OAuth Consent Screen Branding."""
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Terms of Service — YouTube Multilingual Publishing Agent</title>
  <style>{_BRANDING_STYLE}</style>
</head>
<body>
  <div class="container">
    <span class="badge">Terms of Service</span>
    <h1>Terms of Service — YouTube Multilingual Publishing Agent</h1>
    <p><strong>Effective Date:</strong> November 2026</p>
    <p>
      By using <strong>YouTube Multilingual Publishing Agent</strong>, you agree to these
      Terms of Service and to be bound by the
      <a href="https://www.youtube.com/t/terms"
         target="_blank" rel="noopener">YouTube Terms of Service</a>.
    </p>
    <h2>Authorized Channel Use Only</h2>
    <p>
      This application is intended solely for authorized owners and managers of the configured
      YouTube channel to manage multilingual metadata localizations and subtitle tracks.
      All YouTube write operations are gated by deterministic validation and operator policy
      controls.
    </p>
    <div class="nav">
      <a href="/home">Application Homepage</a>
      <a href="/privacy">Privacy Policy</a>
    </div>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)
