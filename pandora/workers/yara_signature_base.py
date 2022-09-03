#!/usr/bin/env python3

import os

from typing import Optional

import yara  # type: ignore

from ..default import get_homedir
from ..helpers import Status
from ..task import Task
from ..report import Report

from .yara import YaraWorker


class YaraSignatureBaseWorker(YaraWorker):
    rulespath = get_homedir() / "yara_repos" / "signature-base"
    savepath = rulespath / "yara.compiled"
    needs_external = [
        "generic_anomalies.yar",
        "general_cloaking.yar",
        "thor_inverse_matches.yar",
        "yara_mixed_ext_vars.yar",
    ]
    last_change: Optional[float] = None

    def rules_with_external_vars(
        self, filename: str, filepath: str, filetype: str, owner: str
    ) -> yara.Rules:
        extension = os.path.splitext(filename)[1]
        yara_files = [
            y_file
            for y_file in self.rulespath.glob("**/*.yar")
            if y_file.name in self.needs_external
        ]
        rules = yara.compile(
            filepaths={str(path): str(path) for path in yara_files},
            includes=True,
            externals={
                "filename": filename,
                "filepath": filepath,
                "extension": extension,
                "filetype": filetype,
                "owner": owner,
            },
        )
        return rules

    def analyse(self, task: Task, report: Report, manual_trigger: bool = False):
        if not task.file.data:
            report.status = Status.NOTAPPLICABLE
            return

        super().analyse(task=task, report=report)

        filetype = task.file.type  # only match in generic_anomalies.yar for "GIF"
        owner = ""  # only match in yara_mixed_ext_vars.yar for "confluence"
        rules_external = self.rules_with_external_vars(
            filename=task.file.original_filename,
            filepath=task.file.original_filename,
            filetype=filetype,
            owner=owner,
        )
        matches = [
            str(match)
            for match in rules_external.match(data=task.file.data.getvalue())
            if match
        ]
        if matches:
            report.status = Status.ALERT
            report.add_details("Rules matches", matches)
