"""phoneloop.browser -- which Chromium build the live scripts drive.

Measured 2026-08-28 from the home IP (86.20.84.13, with Mullvad
split-tunnelled so that ONLY the automation binary bypasses the VPN):
the bundled Playwright Chromium 148 got the eBay 403 "Something went wrong"
page on the homepage, with and without the automation flags suppressed;
Chrome for Testing 152 at the same address got a 200. The build is the
discriminator, so the binary is configurable and the choice lives in the
machine-local secrets file, not in code.
"""
from phoneloop.browser import launch_kwargs


def test_configured_binary_wins():
    kw = launch_kwargs({"PHONELOOP_CHROME_EXE": r"C:\x\chrome-win64\chrome.exe"})
    assert kw == {"executable_path": r"C:\x\chrome-win64\chrome.exe"}


def test_unconfigured_falls_back_to_installed_chrome():
    assert launch_kwargs({}) == {"channel": "chrome"}


def test_blank_value_means_unconfigured():
    assert launch_kwargs({"PHONELOOP_CHROME_EXE": ""}) == {"channel": "chrome"}
