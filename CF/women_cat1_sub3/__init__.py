# Boutiqaat Web Scraper Package — Cloudflare R2 workflow
__version__ = '1.0.0'
__author__ = 'Boutiqaat Data Pipeline'

from .scraper import BoutiqaatScraper
from .r2_uploader import R2Uploader
from .excel_generator import ExcelGenerator

__all__ = ['BoutiqaatScraper', 'R2Uploader', 'ExcelGenerator']
