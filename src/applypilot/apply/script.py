from patchright.sync_api import sync_playwright

with sync_playwright() as p:
    # Path to your Brave browser executable
    # Windows example: r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
    # Mac example: "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
    # Linux example: "/usr/bin/brave-browser"
    brave_path = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"

    browser = p.chromium.launch(
        executable_path=brave_path,
        headless=False
    )
    page = browser.new_page()
    page.goto("https://browserscan.net")

    # Keep it open a moment to see it work
    page.wait_for_timeout(5000)
