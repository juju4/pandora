#!/usr/bin/env python3

import logging
import os
import re
import subprocess

from ..helpers import Status
from ..task import Task
from ..report import Report

from .base import BaseWorker


class ComodoWorker(BaseWorker):

    comodo_path: str
    comodo_bases: str = "/opt/COMODO/scanners/bases.cav"  # this seems to be hardcoded

    def __init__(self, module: str, worker_id: int, cache: str, timeout: str, loglevel: int = logging.INFO, **options):
        super().__init__(module, worker_id, cache, timeout, loglevel, **options)

        if not self.comodo_path or not os.path.exists(self.comodo_path) or not os.path.exists(self.comodo_bases):
            self.disabled = True
            return

    def analyse(self, task: Task, report: Report, manual_trigger: bool = False):
        self.logger.debug(f"analysing file {task.file.path}...")
        args = [self.comodo_path, "-v", "-s", str(task.file.path)]
        process = subprocess.run(args, capture_output=True, timeout=self.timeout, check=False)
        reg = re.compile("(?P<file>.*) ---> Found .*, Malware Name is (?P<name>.*)", re.IGNORECASE)
        for i in reg.finditer(process.stdout.decode()):
            report.status = Status.ALERT
            report.add_details("malicious", i.group(2))
