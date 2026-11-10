"""Browser-based fetching for pages that block basic HTTP requests."""
import logging
import random
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    PlaywrightTimeout = Exception


# Human-like user agents
USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]


def fetch_with_browser(url: str, timeout: int = 30) -> Optional[str]:
    """
    Fetch a page using a headless Chromium browser with human-like parameters.
    
    This is a fallback for pages that block basic HTTP requests or require JavaScript.
    
    Args:
        url: URL to fetch
        timeout: Timeout in seconds
        
    Returns:
        Page text content or None if failed
    """
    if not PLAYWRIGHT_AVAILABLE:
        logger.warning("Playwright not installed. Cannot use browser fallback. Install with: pip install playwright && playwright install chromium")
        return None
    
    try:
        with sync_playwright() as p:
            # Launch browser with human-like settings
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                ]
            )
            
            # Create context with human-like settings
            context = browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={'width': 1920, 'height': 1080},
                locale='en-US',
                timezone_id='America/New_York',
                # Emulate real browser permissions
                permissions=[],
                java_script_enabled=True,
                # Accept language
                extra_http_headers={
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'DNT': '1',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                }
            )
            
            # Add random delays to appear more human
            page = context.new_page()
            
            # Navigate to page
            logger.info(f"Browser fetching: {url}")
            try:
                response = page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')
                
                if not response:
                    logger.warning(f"No response from browser for {url}")
                    browser.close()
                    return None
                
                # Check status
                if response.status >= 400:
                    logger.warning(f"Browser fetch returned status {response.status} for {url}")
                    browser.close()
                    return None
                
                # Wait a bit for any dynamic content (but not too long)
                page.wait_for_timeout(random.randint(1000, 2000))
                
                # Extract text content
                # Remove script, style, nav, footer elements
                page.evaluate("""
                    () => {
                        const selectors = ['script', 'style', 'nav', 'footer', 'header', 'iframe'];
                        selectors.forEach(sel => {
                            document.querySelectorAll(sel).forEach(el => el.remove());
                        });
                    }
                """)
                
                # Get text content
                text = page.evaluate("() => document.body.innerText")
                
                browser.close()
                
                if text and text.strip():
                    logger.info(f"Successfully fetched {len(text)} characters from {url}")
                    return text.strip()
                else:
                    logger.warning(f"Empty content from browser for {url}")
                    return None
                    
            except PlaywrightTimeout:
                logger.warning(f"Browser timeout for {url}")
                browser.close()
                return None
            except Exception as e:
                logger.warning(f"Browser navigation error for {url}: {e}")
                browser.close()
                return None
                
    except Exception as e:
        logger.error(f"Browser fetch failed for {url}: {e}")
        return None


def fetch_multiple_with_browser(urls: list[tuple[str, str]], timeout: int = 30) -> dict[str, Optional[str]]:
    """
    Fetch multiple URLs with the browser, reusing browser instance for efficiency.
    
    Args:
        urls: List of (url, identifier) tuples
        timeout: Timeout per page in seconds
        
    Returns:
        Dict mapping identifier to fetched content (or None if failed)
    """
    if not PLAYWRIGHT_AVAILABLE:
        logger.warning("Playwright not installed. Cannot use browser fallback.")
        return {identifier: None for url, identifier in urls}
    
    results = {}
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                ]
            )
            
            for url, identifier in urls:
                try:
                    context = browser.new_context(
                        user_agent=random.choice(USER_AGENTS),
                        viewport={'width': 1920, 'height': 1080},
                        locale='en-US',
                        timezone_id='America/New_York',
                        extra_http_headers={
                            'Accept-Language': 'en-US,en;q=0.9',
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                            'Accept-Encoding': 'gzip, deflate, br',
                            'DNT': '1',
                            'Connection': 'keep-alive',
                            'Upgrade-Insecure-Requests': '1',
                        }
                    )
                    
                    page = context.new_page()
                    logger.info(f"Browser fetching: {url}")
                    
                    response = page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')
                    
                    if response and response.status < 400:
                        page.wait_for_timeout(random.randint(1000, 2000))
                        
                        page.evaluate("""
                            () => {
                                const selectors = ['script', 'style', 'nav', 'footer', 'header', 'iframe'];
                                selectors.forEach(sel => {
                                    document.querySelectorAll(sel).forEach(el => el.remove());
                                });
                            }
                        """)
                        
                        text = page.evaluate("() => document.body.innerText")
                        
                        if text and text.strip():
                            results[identifier] = text.strip()
                            logger.info(f"Successfully fetched {identifier}: {len(text)} chars")
                        else:
                            results[identifier] = None
                            logger.warning(f"Empty content for {identifier}")
                    else:
                        status = response.status if response else "no response"
                        logger.warning(f"Failed to fetch {identifier}: status {status}")
                        results[identifier] = None
                    
                    context.close()
                    
                except Exception as e:
                    logger.warning(f"Browser fetch failed for {identifier}: {e}")
                    results[identifier] = None
                    try:
                        context.close()
                    except:
                        pass
            
            browser.close()
            
    except Exception as e:
        logger.error(f"Browser batch fetch failed: {e}")
        # Fill remaining with None
        for url, identifier in urls:
            if identifier not in results:
                results[identifier] = None
    
    return results
