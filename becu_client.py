import csv
import io
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, BrowserContext, Page

SESSION_FILE = Path(__file__).parent / "session.json"
SESSION_MAX_AGE = 15 * 60  # 15 minutes in seconds
SUMMARY_URL = "https://onlinebanking.becu.org/BECUBankingWeb/Accounts/Summary.aspx"
ACTIVITY_URL = "https://onlinebanking.becu.org/BECUBankingWeb/Accounts/Activity.aspx"
LOGIN_DOMAIN = "auth.secure.becu.org"

_CHROMIUM_EXECUTABLE = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or None
_launch_kwargs = {"executable_path": _CHROMIUM_EXECUTABLE} if _CHROMIUM_EXECUTABLE else {}


def _is_logged_in(page: Page) -> bool:
    url = page.url
    return LOGIN_DOMAIN not in url and "SystemUnavailable" not in url


async def _login(page: Page) -> None:
    username = os.environ["BECU_USERNAME"]
    password = os.environ["BECU_PASSWORD"]

    await page.goto(SUMMARY_URL)
    await page.wait_for_load_state("load")

    if _is_logged_in(page):
        return

    await page.fill('input[name="username"]', username)
    await page.fill('input[name="password"]', password)
    await page.click('button[type="submit"], input[type="submit"]')
    await page.wait_for_load_state("load")

    if LOGIN_DOMAIN in page.url:
        print("MFA may be required. Please complete authentication in the browser window...")
        await page.wait_for_url(f"**{SUMMARY_URL}**", timeout=60000)
        await page.wait_for_load_state("load", timeout=10000)


async def _save_session(context: BrowserContext) -> None:
    cookies = await context.cookies()
    SESSION_FILE.write_text(json.dumps(cookies))


async def _load_session(context: BrowserContext) -> bool:
    if not SESSION_FILE.exists():
        return False
    age = time.time() - SESSION_FILE.stat().st_mtime
    if age > SESSION_MAX_AGE:
        SESSION_FILE.unlink()
        return False
    try:
        cookies = json.loads(SESSION_FILE.read_text())
        await context.add_cookies(cookies)
        return True
    except Exception:
        return False


async def _with_authenticated_page(callback):
    """Run callback(page) with a headless authenticated page. Re-auths if session expired."""

    async def _do_visible_auth(p):
        browser = await p.chromium.launch(headless=False, **_launch_kwargs)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await _login(page)
            await _save_session(context)
        finally:
            await browser.close()

    async with async_playwright() as p:
        if not SESSION_FILE.exists():
            await _do_visible_auth(p)

        browser = await p.chromium.launch(headless=True, **_launch_kwargs)
        try:
            context = await browser.new_context()
            await _load_session(context)
            page = await context.new_page()

            # Verify session is still valid
            await page.goto(SUMMARY_URL, wait_until="load")
            if not _is_logged_in(page):
                await browser.close()
                await _do_visible_auth(p)
                browser = await p.chromium.launch(headless=True, **_launch_kwargs)
                context = await browser.new_context()
                await _load_session(context)
                page = await context.new_page()

            result = await callback(page)
            await _save_session(context)
            return result
        finally:
            await browser.close()


async def _export_transactions_csv(page: Page, account_index: int, start_date: str, end_date: str) -> list[dict]:
    """
    Navigate to the BECU activity page, grab the ASP.NET form tokens, then POST
    directly for a CSV download (bypasses the AJAX UpdatePanel UI entirely).
    """
    activity_url = f"{ACTIVITY_URL}?index={account_index}"
    await page.goto(activity_url, wait_until="load")
    await page.wait_for_timeout(500)

    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    def _val(name: str) -> str:
        el = soup.find("input", {"name": name})
        return el["value"] if el else ""

    vs = _val("__VIEWSTATE")
    vsg = _val("__VIEWSTATEGENERATOR")
    ev = _val("__EVENTVALIDATION")

    if not vs:
        raise RuntimeError("Could not extract VIEWSTATE from BECU activity page")

    response = await page.request.post(
        activity_url,
        form={
            "__VIEWSTATE": vs,
            "__VIEWSTATEGENERATOR": vsg,
            "__EVENTVALIDATION": ev,
            "DES_Group": "DOWNLOAD",
            "ddlAccounts": str(account_index),
            "ddlType": "DateRange",
            "txtFromDate$TextBox": start_date,
            "txtToDate$TextBox": end_date,
            "cboDownloadTypeList": "csv",
            "BtnDownload": "Download",
        },
        headers={"Referer": activity_url},
        timeout=90000,
    )

    if response.status != 200:
        raise RuntimeError(f"BECU CSV download returned HTTP {response.status}")

    body = await response.body()
    if not body:
        raise RuntimeError("BECU CSV download returned empty response")

    return _parse_becu_csv_bytes(body)


def _parse_becu_csv_bytes(data: bytes) -> list[dict]:
    """Parse BECU CSV bytes into transaction dicts."""
    content = data.decode("utf-8-sig", errors="replace")
    lines = content.splitlines()
    header_idx = next(
        (i for i, line in enumerate(lines) if re.search(r"\bDate\b", line, re.IGNORECASE)),
        0,
    )
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    transactions = []
    for row in reader:
        txn: dict = {}
        for key, val in row.items():
            if not key:
                continue
            k = key.strip().lower()
            v = val.strip() if val else ""
            if "date" in k and "date" not in txn:
                txn["date"] = v
            elif "description" in k:
                txn["description"] = v
            elif k == "amount":
                txn["amount"] = _parse_currency(v)
            elif any(w in k for w in ("withdrawal", "debit", "charge")):
                if v:
                    txn.setdefault("amount", -abs(_parse_currency(v) or 0))
            elif any(w in k for w in ("deposit", "credit")):
                if v and "amount" not in txn:
                    txn["amount"] = abs(_parse_currency(v) or 0)
            elif "balance" in k:
                txn["balance"] = _parse_currency(v)
        if txn.get("date") and re.search(r"\d", txn.get("date", "")):
            transactions.append(txn)
    return transactions


def _parse_currency(text: str) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"-?\$?([\d,]+\.?\d*)", text.strip())
    if not match:
        return None
    cleaned = match.group(1).replace(",", "")
    if text.strip().startswith("-") or "-$" in text:
        cleaned = "-" + cleaned
    try:
        return float(cleaned)
    except ValueError:
        return None


def _cell_label_and_value(cell) -> tuple[str, str]:
    label_el = cell.find("b", class_="tablesaw-cell-label")
    label = label_el.get_text(strip=True).lower() if label_el else ""
    if label_el:
        label_el.extract()
    value = cell.get_text(strip=True)
    return label, value


async def get_accounts() -> list[dict]:
    """Scrape account summary page and return all accounts with balances."""
    async def _scrape(page: Page) -> list[dict]:
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        accounts = []
        seen_account_numbers: set[str] = set()

        for table in soup.select("table"):
            if not table.find("a", href=re.compile(r"index=\d+|loanId=")):
                continue

            for row in table.select("tr.item, tr.alternatingItem"):
                link = None
                cell_data: dict[str, str] = {}
                for cell in row.select("td"):
                    label, value = _cell_label_and_value(cell)
                    cell_data[label] = value
                    if not link:
                        link = cell.find("a")

                if not link:
                    continue

                name = link.get_text(strip=True)
                href = link.get("href", "")

                idx_match = re.search(r"index=(\d+)", href)
                account_index = int(idx_match.group(1)) if idx_match else None

                parts = name.rsplit(" ", 1)
                account_number = parts[-1] if len(parts) > 1 and parts[-1].isdigit() else None
                display_name = parts[0] if account_number else name

                dedup_key = account_number or name
                if dedup_key in seen_account_numbers:
                    continue
                seen_account_numbers.add(dedup_key)

                account = {
                    "index": account_index,
                    "name": display_name,
                    "account_number": account_number,
                    "full_name": name,
                    "current_balance": _parse_currency(cell_data.get("current balance", "")),
                    "available_balance": _parse_currency(cell_data.get("available balance", "")),
                    "ytd_interest": _parse_currency(cell_data.get("ytd interest", "")),
                }
                accounts.append(account)

        return accounts

    return await _with_authenticated_page(_scrape)


async def get_balance(account_index: int) -> Optional[dict]:
    """Return balance info for a single account by its index."""
    accounts = await get_accounts()
    for acct in accounts:
        if acct.get("index") == account_index:
            return acct
    return None


async def get_transactions(
    account_index: int,
    days: int = 30,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """
    Export transactions for an account using BECU's CSV download.

    Args:
        account_index: The account index (from get_accounts).
        days: Number of days back from today (used if start_date/end_date not provided).
        start_date: Start of date range in MM/DD/YYYY format (overrides days).
        end_date: End of date range in MM/DD/YYYY format (overrides days).
    """
    if not end_date:
        end_date = datetime.now().strftime("%m/%d/%Y")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=days)).strftime("%m/%d/%Y")

    async def _do_export(page: Page) -> list[dict]:
        return await _export_transactions_csv(page, account_index, start_date, end_date)

    return await _with_authenticated_page(_do_export)
