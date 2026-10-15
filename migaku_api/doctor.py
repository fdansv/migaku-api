"""Offline installation checks; does not open a browser or inspect account/session data."""
import argparse
from importlib.metadata import version
import json
from pathlib import Path
import platform

from playwright.sync_api import sync_playwright

from .browser import (ENRICHMENT_ASSETS, EXPERIMENTAL_VERSION, WorkerError, discover_core,
                      enrichment_compatible, experimental_profile_enabled, find_extension)


def inspect_extension(extension):
    report = {'installed_version': None, 'rpc_factory_found': False,
              'card_enrichment_and_translation_compatible': False,
              'supported_enrichment_versions': [v for v in ENRICHMENT_ASSETS if v != EXPERIMENTAL_VERSION],
              'experimental_enrichment_versions': [EXPERIMENTAL_VERSION]}
    if not extension:
        report['error'] = 'No extension found. Set MIGAKU_EXTENSION_PATH to your installed extension directory.'
        return report
    try:
        core = discover_core(extension)
        report['installed_version'] = core['version']
        report['rpc_factory_found'] = True
        report['assets_match'] = enrichment_compatible(extension, core['version'])
        report['experimental_profile'] = core['version'] == EXPERIMENTAL_VERSION
        report['experimental_profile_enabled'] = experimental_profile_enabled()
        report['card_enrichment_and_translation_compatible'] = bool(report['assets_match'] and
            (not report['experimental_profile'] or report['experimental_profile_enabled']))
        if not report['card_enrichment_and_translation_compatible']:
            report['error'] = ('Experimental 1.31.0.17 assets match, but the profile is disabled; see MIGAKU_ENABLE_EXPERIMENTAL_131017 in the setup guide.'
                if report['assets_match'] else
                'The installed extension needs an adapter compatibility review; installing it alone will not enable card enrichment.')
    except (OSError, ValueError, KeyError, WorkerError) as error:
        report['error'] = str(error)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extension', type=Path, help='Explicit installed extension directory')
    args = parser.parse_args()
    extension = args.extension.expanduser().resolve() if args.extension else find_extension()
    report = {'python': platform.python_version(), 'platform': platform.system(),
              'playwright': version('playwright'), 'extension': inspect_extension(extension)}
    with sync_playwright() as playwright:
        report['chromium_downloaded'] = Path(playwright.chromium.executable_path).is_file()
    report['ready_for_first_login'] = bool(report['chromium_downloaded'] and
        report['extension']['card_enrichment_and_translation_compatible'])
    report['scope'] = 'Static checks only. Login, initial cloud sync, browser OS dependencies and live private RPCs still need verification through /worker.'
    print(json.dumps(report, indent=2))
    return 0 if report['ready_for_first_login'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
