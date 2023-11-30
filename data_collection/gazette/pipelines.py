import datetime as dt
from os import PathLike
from pathlib import Path
from typing import Union

import filetype
from itemadapter import ItemAdapter
from scrapy import spiderloader
from scrapy.exceptions import DropItem
from scrapy.http import Request
from scrapy.http.request import NO_CALLBACK
from scrapy.pipelines.files import FilesPipeline, FSFilesStore
from scrapy.settings import Settings
from scrapy.utils import project
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from gazette.database.models import Gazette, initialize_database


class GazetteDateFilteringPipeline:
    def process_item(self, item, spider):
        if hasattr(spider, "start_date"):
            if spider.start_date > item.get("date"):
                raise DropItem("Droping all items before {}".format(spider.start_date))
        return item


class DefaultValuesPipeline:
    def process_item(self, item, spider):
        item["territory_id"] = getattr(spider, "TERRITORY_ID")

        # Date manipulation to allow jsonschema to validate correctly
        item["date"] = str(item["date"])
        item["scraped_at"] = dt.datetime.utcnow().isoformat("T") + "Z"

        return item


class SQLDatabasePipeline:
    def __init__(self, database_url):
        self.database_url = database_url

    @classmethod
    def from_crawler(cls, crawler):
        database_url = crawler.settings.get("QUERIDODIARIO_DATABASE_URL")
        return cls(database_url=database_url)

    def _generate_territory_spider_map(self):
        settings = project.get_project_settings()
        spider_loader = spiderloader.SpiderLoader.from_settings(settings)
        spiders = spider_loader.list()
        classes = [spider_loader.load(name) for name in spiders]

        mapping = []
        for spider_class in classes:
            spider_name = getattr(spider_class, "name", None)
            territory_id = getattr(spider_class, "TERRITORY_ID", None)
            date_from = getattr(spider_class, "start_date", None)
            if all((spider_name, territory_id, date_from)):
                mapping.append((spider_name, territory_id, date_from))
        return mapping

    def open_spider(self, spider):
        if self.database_url is not None:
            territory_spider_map = self._generate_territory_spider_map()
            engine = initialize_database(self.database_url, territory_spider_map)
            self.Session = sessionmaker(bind=engine)

    def process_item(self, item, spider):
        if self.database_url is None:
            return item

        session = self.Session()

        fields = [
            "source_text",
            "date",
            "edition_number",
            "is_extra_edition",
            "power",
            "scraped_at",
            "territory_id",
        ]
        gazette_item = {field: item.get(field) for field in fields}
        gazette_item["date"] = dt.datetime.strptime(
            gazette_item["date"], "%Y-%m-%d"
        ).date()
        gazette_item["scraped_at"] = dt.datetime.strptime(
            gazette_item["scraped_at"], "%Y-%m-%dT%H:%M:%S.%fZ"
        )

        for file_info in item.get("files", []):
            already_downloaded = file_info["status"] == "uptodate"
            if already_downloaded:
                # We should not insert in database information of
                # files that were already downloaded before
                continue

            gazette_item["file_path"] = file_info["path"]
            gazette_item["file_url"] = file_info["url"]
            gazette_item["file_checksum"] = file_info["checksum"]

            gazette = Gazette(**gazette_item)
            session.add(gazette)
            try:
                session.commit()
            except SQLAlchemyError as exc:
                spider.logger.warning(
                    f"Something wrong has happened when adding the gazette in the database. "
                    f"Date: {gazette_item['date']}. "
                    f"File Checksum: {gazette_item['file_checksum']}. "
                    f"Details: {exc.args}"
                )
                session.rollback()

        session.close()

        return item


class QueridoDiarioFilesPipeline(FilesPipeline):
    """Pipeline to download files described in file_urls or file_requests item fields.

    The main differences from the default FilesPipelines is that this pipeline:
        - organizes downloaded files differently (based on territory_id)
        - adds the file_requests item field to download files from request instances
        - allows a download_file_headers spider attribute to modify file_urls requests
    """

    DEFAULT_FILES_REQUESTS_FIELD = "file_requests"

    def __init__(self, *args, settings=None, **kwargs):
        self.STORE_SCHEMES[""] = QueridoDiarioFSFilesStore
        self.STORE_SCHEMES["file"] = QueridoDiarioFSFilesStore
        super().__init__(*args, settings=settings, **kwargs)

        if isinstance(settings, dict) or settings is None:
            settings = Settings(settings)

        self.files_requests_field = settings.get(
            "FILES_REQUESTS_FIELD", self.DEFAULT_FILES_REQUESTS_FIELD
        )

    def get_media_requests(self, item, info):
        """Makes requests from urls and/or lets through ready requests."""
        urls = ItemAdapter(item).get(self.files_urls_field, [])
        download_file_headers = getattr(info.spider, "download_file_headers", {})
        yield from (
            Request(u, callback=NO_CALLBACK, headers=download_file_headers)
            for u in urls
        )

        requests = ItemAdapter(item).get(self.files_requests_field, [])
        yield from requests

    def item_completed(self, results, item, info):
        """
        Transforms requests into strings if any present.
        Default behavior also adds results to item.
        """
        requests = ItemAdapter(item).get(self.files_requests_field, [])
        if requests:
            ItemAdapter(item)[self.files_requests_field] = [
                f"{r.method} {r.url}" for r in requests
            ]

        return super().item_completed(results, item, info)

    def file_path(self, request, response=None, info=None, item=None):
        """
        Path to save the files, modified to organize the gazettes in directories.
        The files will be under <territory_id>/<gazette date>/.
        """
        filepath = Path(
            super().file_path(request, response=response, info=info, item=item)
        )
        # The default path from the scrapy class begins with "full/". In this
        # class we replace that with the territory_id and gazette date.
        filename = filepath.name

        if not filepath.suffix and response is not None:
            extension = self._detect_extension(response)
            if extension:
                filename += f".{extension}"

        return str(Path(item["territory_id"], item["date"], filename))

    def _detect_extension(self, response):
        """Checks file extension from file header if possible"""
        max_file_header_size = 261
        file_kind = filetype.guess(response.body[:max_file_header_size])
        if file_kind is None:
            # logger.warning(f"Unable to guess the file type from downloaded file {response}!")
            return ""

        return file_kind.extension


class QueridoDiarioFSFilesStore(FSFilesStore):
    def __init__(self, basedir: Union[str, PathLike]):
        super().__init__(basedir)

    def stat_file(self, path: Union[str, PathLike], info):
        path_obj = Path(path)
        if path_obj.suffix:
            return super().stat_file(path, info)

        path_with_ext = self._find_file(path_obj)
        return super().stat_file(path_with_ext, info)

    def _find_file(self, path):
        """Finds a file with extension from a file path without extension"""
        absolute_path = Path(self.basedir, path)
        files = [p for p in absolute_path.parent.glob(f"{path.name}.*")]
        if len(files) > 0:
            return Path(path.parent, files[0].name)
        return path
