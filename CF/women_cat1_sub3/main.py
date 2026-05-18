import asyncio
import logging
from typing import Dict, List
import os
import shutil
from datetime import datetime
from collections import defaultdict

from .scraper import BoutiqaatScraper
from .r2_uploader import R2Uploader
from .excel_generator import ExcelGenerator
from cf_config import TEMP_DIR, R2_EXCEL_PATH

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Hardcoded subcategory URLs for Group 3
SUBCATEGORY_URLS = [
    # --- merged from original sub5 ---
    "https://www.boutiqaat.com/ar-kw/women/makeup/brush-holders-accessories/l/",
    "https://www.boutiqaat.com/ar-kw/women/makeup/makeup-bags/l/",
    "https://www.boutiqaat.com/ar-kw/women/makeup/mirrors/l/",
    "https://www.boutiqaat.com/ar-kw/women/makeup/eyeshadow-primers/l/",
    "https://www.boutiqaat.com/ar-kw/women/makeup/makeup-brush-cleaners/l/",
    "https://www.boutiqaat.com/ar-kw/women/makeup/face-palettes/l/",
    "https://www.boutiqaat.com/ar-kw/women/makeup/lip-care/l/",
    "https://www.boutiqaat.com/ar-kw/women/makeup/eye-palettes/l/",
]


class BoutiqaatDataPipeline:
    """Main orchestrator for scraping, processing, and uploading data to Cloudflare R2"""

    def __init__(self):
        self.uploader = R2Uploader()
        self.excel_generator = ExcelGenerator()

    async def _process_url_async(self, semaphore: asyncio.Semaphore, url: str) -> bool:
        """Acquire semaphore slot and scrape one subcategory URL in a thread."""
        async with semaphore:
            category_name = url.rstrip('/').split('/')[-2]
            category_dict = {'name': category_name, 'url': url}
            logger.info(f"[Slot acquired] Starting: {category_name}")
            scraper = BoutiqaatScraper()
            try:
                return await asyncio.to_thread(
                    self._process_category, scraper, category_dict
                )
            except Exception as e:
                logger.error(f"Error in {category_name}: {str(e)}")
                return False

    def run(self) -> bool:
        """Gather all subcategory URLs concurrently, max 3 at a time."""
        logger.info("=" * 80)
        logger.info("Starting Boutiqaat Data Pipeline — Cloudflare R2 (Async – Semaphore=3)")
        logger.info("=" * 80)
        try:
            if not self.uploader.test_connection():
                logger.error("R2 connection failed. Exiting.")
                return False

            logger.info(
                f"Processing {len(SUBCATEGORY_URLS)} subcategories "
                f"(max 3 concurrent)"
            )
            semaphore = asyncio.Semaphore(3)

            async def _gather_all():
                return await asyncio.gather(
                    *[self._process_url_async(semaphore, url) for url in SUBCATEGORY_URLS],
                    return_exceptions=True,
                )
            results = asyncio.run(_gather_all())

            successful = sum(1 for r in results if r is True)
            failed = len(results) - successful
            logger.info("=" * 80)
            logger.info(
                f"Pipeline Complete: {successful} successful, {failed} failed"
            )
            logger.info("=" * 80)
            return failed == 0
        except Exception as e:
            logger.error(f"Pipeline failed: {str(e)}")
            return False
        finally:
            import shutil as _shutil
            if os.path.exists(TEMP_DIR):
                try:
                    _shutil.rmtree(TEMP_DIR)
                    logger.info("Cleaned up temporary files")
                except Exception as exc:
                    logger.warning(f"Failed to cleanup temp files: {exc}")

    def _process_category(self, scraper: BoutiqaatScraper, category: dict) -> bool:
        """Process a single category and extract products"""
        category_name = category['name']
        category_url = category['url']

        logger.info(f"\n--- Processing Category: {category_name} ---")

        try:
            products = scraper.get_products(category_url)

            if not products:
                logger.warning(f"No products found for {category_name}, skipping.")
                return True

            logger.info(f"Found {len(products)} total products in category")

            subcategories_data = defaultdict(list)

            for product in products:
                subcategory = product.get('subcategory', category_name)
                subcategories_data[subcategory].append(product)

            for subcategory_name, products_in_sub in subcategories_data.items():
                logger.info(f"  Processing Subcategory: {subcategory_name} ({len(products_in_sub)} products)")

                for idx, product in enumerate(products_in_sub, 1):
                    logger.info(f"    [{idx}/{len(products_in_sub)}] Processing: {product.get('name', 'Unknown')}")

                    try:
                        full_details = scraper.get_product_full_details(product['url'])
                        if full_details:
                            product.update(full_details)

                        if product.get('image_url'):
                            r2_image_path = self._upload_product_image(
                                product,
                                category_name,
                                subcategory_name
                            )
                            product['s3_image_path'] = r2_image_path
                        else:
                            product['s3_image_path'] = 'No image available'

                    except Exception as e:
                        logger.warning(f"    Error processing product: {str(e)}")
                        continue

            if subcategories_data:
                excel_file = self.excel_generator.create_category_workbook(
                    category_name,
                    subcategories_data
                )

                self._upload_excel_file(excel_file, category_name)

            logger.info(f"✓ Completed category: {category_name}")
            return True

        except Exception as e:
            logger.error(f"✗ Failed category {category_name}: {str(e)}")
            return False

    def _upload_product_image(self, product: Dict, category_name: str, subcategory_name: str) -> str:
        """Download and upload product image to Cloudflare R2"""
        try:
            image_url = product.get('image_url')
            sku = product.get('sku', 'unknown')

            if not image_url:
                return 'No image URL'

            # Sanitize names for R2 path
            safe_category = "".join(c for c in category_name if c.isalnum() or c in (' ', '_')).rstrip()

            # R2 path: boutiqaat-data/year=YYYY/month=MM/day=DD/women-makeup/images/category/
            r2_path = (
                f"boutiqaat-data/year={datetime.now().strftime('%Y')}/month={datetime.now().strftime('%m')}/day={datetime.now().strftime('%d')}/women-makeup/images/"
                f"{safe_category}"
            )

            filename = f"{sku}_image.jpg"

            r2_key = self.uploader.upload_image_from_url(
                image_url,
                filename,
                r2_path
            )

            return r2_key if r2_key else 'Upload failed'

        except Exception as e:
            logger.warning(f"Error uploading image for {product.get('name')}: {str(e)}")
            return 'Error'

    def _upload_excel_file(self, local_path: str, category_name: str) -> bool:
        """Upload Excel file to Cloudflare R2"""
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{category_name}_{timestamp}.xlsx"

            # R2 path: boutiqaat-data/year=YYYY/month=MM/day=DD/women-makeup/excel-files/
            r2_path = (
                f"boutiqaat-data/year={datetime.now().strftime('%Y')}/month={datetime.now().strftime('%m')}/day={datetime.now().strftime('%d')}/women-makeup/excel-files"
            )

            r2_key = self.uploader.upload_local_file(
                local_path,
                r2_path,
                filename
            )

            if r2_key:
                logger.info(f"Excel file uploaded: {r2_key}")
                return True
            else:
                logger.error(f"Failed to upload Excel file: {local_path}")
                return False

        except Exception as e:
            logger.error(f"Error uploading Excel file: {str(e)}")
            return False


def main():
    """Entry point for the pipeline"""
    pipeline = BoutiqaatDataPipeline()
    success = pipeline.run()
    return 0 if success else 1


if __name__ == '__main__':
    exit(main())
