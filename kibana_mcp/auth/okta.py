import asyncio
import json
import os
import sys
import time
from typing import Optional

from kibana_mcp.config import config
from kibana_mcp.auth.session import Session, save_session

PUSH_POLL_INTERVAL = 3.0
PUSH_POLL_TIMEOUT = 180.0


async def _get_saml_redirect_url_via_browser(page) -> str:
    """
    Call Kibana's /internal/security/login from INSIDE the browser so that
    Kibana can set its relay-state cookie on the same browser context.
    Returns the Okta redirect URL.
    """
    kibana_url = config.kibana.base_url
    body = {
        "providerType": "saml",
        "providerName": "saml1",
        "currentURL": f"{kibana_url}/login?next=/",
    }
    result = await page.evaluate(
        """async ([url, body, kbnVersion]) => {
            const resp = await fetch(url, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "kbn-xsrf": "true",
                    "kbn-version": kbnVersion,
                },
                body: JSON.stringify(body),
                credentials: "include",
            });
            const text = await resp.text();
            return { status: resp.status, body: text };
        }""",
        [f"{kibana_url}/internal/security/login", body, config.kibana.kbn_version],
    )
    if result["status"] >= 400:
        raise RuntimeError(f"SAML initiation failed: {result['status']} {result['body'][:200]}")
    data = json.loads(result["body"])
    redirect = (
        data.get("location")
        or data.get("redirectURL")
        or data.get("redirect")
    )
    if not redirect and isinstance(data, dict):
        for v in data.values():
            if isinstance(v, str) and v.startswith("http"):
                redirect = v
                break
    if not redirect:
        raise RuntimeError(f"No redirect URL in SAML login response: {json.dumps(data)[:300]}")
    return redirect


async def login_with_okta() -> Session:
    """Open a visible browser window for the user to complete Okta login manually.

    No credentials are passed — the user types them in the real browser and
    approves the Okta Verify push themselves. The SAML relay-state cookie is
    established via an in-browser fetch before the user is handed control.
    Once they land on Kibana, we capture the session and Okta cookies so
    silent refresh can take over.
    """
    from playwright.async_api import async_playwright

    print("[auth] Opening browser for Okta SAML login — complete sign-in in the window that appears...", file=sys.stderr)
    okta_domain = config.okta.org.rstrip("/").split("//")[-1]

    async with async_playwright() as p:
        # Always visible — this is the whole point. User drives the login.
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page = await context.new_page()
            # Hide webdriver flag to avoid bot detection
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            # Step 1: navigate to Kibana login page so the browser has the right origin
            # for the fetch() call that initiates SAML (relay state must live in the browser's cookie jar)
            print("[auth] Loading Kibana login page to establish browser origin...", file=sys.stderr)
            await page.goto(f"{config.kibana.base_url}/login", wait_until="domcontentloaded", timeout=30_000)

            # Step 2: call /internal/security/login from inside the browser to set relay-state cookie
            print("[auth] Initiating SAML redirect from browser context...", file=sys.stderr)
            okta_url = await _get_saml_redirect_url_via_browser(page)
            print(f"[auth] Navigating to Okta: {okta_url[:80]}...", file=sys.stderr)

            # Step 3: navigate the browser to Okta — user takes over from here
            await page.goto(okta_url, wait_until="domcontentloaded", timeout=30_000)
            print(f"[auth] Browser opened on Okta — waiting for you to sign in and approve the push...", file=sys.stderr)
            print("[auth] Sign in and approve the Okta Verify push, then this will continue automatically.", file=sys.stderr)

            # Wait up to 5 minutes for the user to complete the full login flow
            await _wait_for_kibana_landing(page, okta_domain, timeout=300.0)
            print("[auth] Login detected — capturing session cookies...", file=sys.stderr)

            kibana_cookies = await context.cookies(config.kibana.base_url)
            okta_cookies = await context.cookies(config.okta.org)

            sid_cookie = next((c for c in kibana_cookies if c["name"] == "sid"), None)
            if not sid_cookie:
                raise RuntimeError("sid cookie not found after Kibana SAML login")

            expires_at = await _get_session_expiry(context)

            session = Session(
                cookie_name="sid",
                cookie_value=sid_cookie["value"],
                expires_at=expires_at,
                okta_cookies=[
                    {
                        "name": c["name"], "value": c["value"], "domain": c["domain"],
                        "path": c["path"], "expires": c.get("expires"), "httpOnly": c.get("httpOnly", False),
                        "secure": c.get("secure", True), "sameSite": c.get("sameSite", "Lax"),
                    }
                    for c in okta_cookies
                ],
            )
            save_session(session)
            return session
        finally:
            await browser.close()


async def try_silent_refresh(session: Session) -> Optional[Session]:
    if not session.okta_cookies:
        return None
    try:
        return await _refresh_with_okta_cookies(session.okta_cookies)
    except Exception as e:
        print(f"[auth] Silent refresh failed: {e}", file=sys.stderr)
        return None


async def _select_push_notification(page) -> None:
    """
    After password submission Okta may show an authenticator selection screen.
    Try several known selectors for Okta Verify push; if none found, assume push
    was sent automatically (happens when only one authenticator is enrolled).
    """
    # Known selectors across different Okta IDX widget versions
    push_selectors = [
        '[data-se="okta_verify-push"]',
        '[data-testid="okta_verify-push"]',
        'button:has-text("Okta Verify")',
        '[aria-label*="Okta Verify"]',
        'a[href*="okta_verify"]',
    ]
    for selector in push_selectors:
        try:
            el = await page.wait_for_selector(selector, timeout=4_000)
            if el:
                print(f"[auth] Authenticator selector found ({selector}) — selecting Okta Verify push...", file=sys.stderr)
                await el.click()
                await asyncio.sleep(1)
                print("[auth] Push notification selected", file=sys.stderr)
                return
        except Exception:
            continue

    print("[auth] No authenticator selector found — assuming push was sent automatically", file=sys.stderr)


async def _wait_for_kibana_landing(page, okta_domain: str, timeout: float = PUSH_POLL_TIMEOUT) -> None:
    """Wait for Playwright to land back on Kibana after Okta push approval."""
    print(f"[auth] Waiting up to {round(timeout / 60)} minutes for login completion...", file=sys.stderr)
    print(f"[auth] Current page before wait: {page.url}", file=sys.stderr)

    # Listen for navigations to debug where the browser goes
    def on_framenavigated(frame):
        if frame == page.main_frame:
            print(f"[auth] Navigation -> {frame.url}", file=sys.stderr)
    page.on("framenavigated", on_framenavigated)

    # Wait until the browser leaves Okta and reaches any Kibana page
    try:
        await page.wait_for_url(
            lambda url: url.startswith(config.kibana.base_url),
            timeout=timeout * 1000,
        )
    except Exception as wait_err:
        url = page.url
        print(f"[auth] wait_for_url timed out — current URL: {url}", file=sys.stderr)
        # Take a screenshot for debugging
        try:
            screenshot_path = os.path.join(os.path.dirname(__file__), "..", "..", "okta_debug.png")
            await page.screenshot(path=os.path.abspath(screenshot_path))
            print(f"[auth] Screenshot saved to {os.path.abspath(screenshot_path)}", file=sys.stderr)
        except Exception:
            pass
        if not url.startswith(config.kibana.base_url):
            raise RuntimeError(
                f"Login timed out after {round(timeout / 60)} minutes — please try again"
            )

    print(f"[auth] Back on Kibana: {page.url}", file=sys.stderr)

    # Kibana may land on the space selector — navigate directly to the configured space
    if "space_selector" in page.url or page.url.rstrip("/") == config.kibana.base_url:
        space = config.kibana.space_id or "default"
        target = f"{config.kibana.base_url}/s/{space}/app/home" if space != "default" else f"{config.kibana.base_url}/app/home"
        print(f"[auth] Space selector detected — navigating to space '{space}'...", file=sys.stderr)
        await page.goto(target, wait_until="domcontentloaded", timeout=20_000)

    if "/login" in page.url:
        raise RuntimeError("SAML callback failed — Kibana redirected back to login page")

    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass


async def _get_session_expiry(context) -> int:
    """Fetch /internal/security/session to get actual expiry from Kibana."""
    import httpx
    kibana_cookies = await context.cookies(config.kibana.base_url)
    sid = next((c for c in kibana_cookies if c["name"] == "sid"), None)
    if not sid:
        return int(time.time() * 1000) + 60 * 60 * 1000  # fallback: 1 hour

    space = config.kibana.space_id
    path = f"/s/{space}/internal/security/session" if space else "/internal/security/session"
    url = f"{config.kibana.base_url}{path}"
    headers = {
        "Cookie": f"sid={sid['value']}",
        "kbn-version": config.kibana.kbn_version,
        "kbn-xsrf": "true",
    }
    try:
        async with httpx.AsyncClient(verify=config.kibana.tls_verify, timeout=10) as client:
            resp = await client.get(url, headers=headers)
        if resp.is_success:
            data = resp.json()
            expires_in_ms = data.get("expiresInMs", 60 * 60 * 1000)
            return int(time.time() * 1000) + expires_in_ms
    except Exception as e:
        print(f"[auth] Could not fetch session expiry: {e}", file=sys.stderr)
    return int(time.time() * 1000) + 60 * 60 * 1000


async def _refresh_with_okta_cookies(okta_cookies: list) -> Optional[Session]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.add_cookies([
                {
                    "name": c["name"], "value": c["value"], "domain": c["domain"],
                    "path": c["path"], "expires": c.get("expires", -1),
                    "httpOnly": c.get("httpOnly", False), "secure": c.get("secure", True),
                    "sameSite": c.get("sameSite", "Lax"),
                }
                for c in okta_cookies
            ])
            page = await context.new_page()
            # Load Kibana login page first, then initiate SAML from within the browser
            await page.goto(f"{config.kibana.base_url}/login", wait_until="domcontentloaded", timeout=30_000)
            okta_url = await _get_saml_redirect_url_via_browser(page)
            await page.goto(okta_url, wait_until="domcontentloaded", timeout=30_000)
            # With valid Okta cookies, this should SSO silently (no MFA)
            await _wait_for_kibana_landing(page, config.okta.org.rstrip("/").split("//")[-1])

            final_url = page.url
            if "/login" in final_url or "okta" in final_url:
                raise RuntimeError("Silent refresh did not land on Kibana — Okta session expired")

            kibana_cookies = await context.cookies(config.kibana.base_url)
            new_okta_cookies = await context.cookies(config.okta.org)

            sid_cookie = next((c for c in kibana_cookies if c["name"] == "sid"), None)
            if not sid_cookie:
                raise RuntimeError("No sid cookie after silent refresh")

            expires_at = await _get_session_expiry(context)

            session = Session(
                cookie_name="sid",
                cookie_value=sid_cookie["value"],
                expires_at=expires_at,
                okta_cookies=[
                    {
                        "name": c["name"], "value": c["value"], "domain": c["domain"],
                        "path": c["path"], "expires": c.get("expires"), "httpOnly": c.get("httpOnly", False),
                        "secure": c.get("secure", True), "sameSite": c.get("sameSite", "Lax"),
                    }
                    for c in new_okta_cookies
                ],
            )
            save_session(session)
            print("[auth] Silent refresh succeeded", file=sys.stderr)
            return session
        finally:
            await browser.close()
