from typing import Dict, Any, Optional, Union, List, overload
from uuid import uuid4

from .file import File
from .user import User
from .helpers import Status, workers
from .observable import TaskObservable, Observable
from .report import Report
from .storage_client import Storage


class Task:

    @overload
    def __init__(self, uuid: Optional[str]=None, submitted_file: Optional[File]=None,
                 user=None, user_id=None, save_date=None,
                 parent=None, origin=None, status: Optional[Union[str, Status]]=None,
                 done: bool=False,
                 disabled_workers=[]):
        ...

    @overload
    def __init__(self, uuid: Optional[str]=None, file_id: Optional[str]=None,
                 user=None, user_id=None, save_date=None,
                 parent=None, origin=None, status: Optional[Union[str, Status]]=None,
                 done: bool=False,
                 disabled_workers=[]):
        ...

    def __init__(self, uuid=None, submitted_file=None, file_id=None,
                 user=None, user_id=None, save_date=None,
                 parent=None, origin=None, status=None,
                 done=False,
                 disabled_workers=[]):
        """
        Generate a Task object.
        :param uuid: Unique identifier of the task.
        :param file: File object
        :param user: User object
        :param save_date: task save date
        :param parent: parent task if file has been extracted
        :param origin: origin task if file has been extracted (can be parent or grand-parent, ...)
        """
        self.storage = Storage()

        if uuid:
            # Loading existing task
            self.uuid = uuid
        else:
            # New task
            self.uuid = str(uuid4())

        assert submitted_file is not None or file_id is not None, 'submitted_file or file_id is required'

        if submitted_file:
            self.file = submitted_file
            self.file_id = self.file.uuid
            self.save_date = self.file.save_date
        elif file_id:
            self.file_id = file_id
            self.file = File(**self.storage.get_file(file_id))
            self.save_date = self.file.save_date
        else:
            self.save_date = save_date

        if user:
            self.user = user
        elif user_id:
            user = self.storage.get_user(user_id)
            if user:
                self.user = User(**user)

        self.observables: List[Observable] = []
        self.parent = parent
        self.origin = origin
        if isinstance(status, Status):
            self._status = status
        elif isinstance(status, str):
            self._status = Status[status]
        else:
            self._status = Status.WAITING
        self.done = done
        self.linked_tasks = None
        self.extracted_tasks = None
        self.disabled_workers = disabled_workers

        # NOTE: this may need to be moved somewhere else
        if self.file.deleted:
            self.status = Status.DELETED

    @property
    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in {
            'uuid': self.uuid,
            'parent_id': self.parent.uuid if self.parent else None,
            'origin_id': self.origin.uuid if self.origin else None,
            'file_id': self.file.uuid if self.file else None,
            'user_id': self.user.get_id() if hasattr(self, 'user') else None,
            'status': self.status.name,
            'save_date': self.save_date.isoformat()
        }.items() if v is not None}

    @property
    def store(self):
        self.storage.set_task(self.to_dict)

    @property
    def reports(self) -> Dict[str, Report]:
        to_return: Dict[str, Report] = {}
        for worker_name in workers():
            if worker_name in self.disabled_workers:
                continue
            stored_report = self.storage.get_report(task_uuid=self.uuid, worker_name=worker_name)
            if stored_report:
                report = Report(**stored_report)
            else:
                report = Report(self.uuid, worker_name)
            to_return[worker_name] = report
        return to_return

    @property
    def workers_done(self) -> bool:
        for report_name, report in self.reports.items():
            if not report.is_done:
                return False
        return True

    @property
    def workers_status(self) -> Dict[str, bool]:
        to_return: Dict[str, bool] = {}
        for report_name, report in self.reports.items():
            to_return[report_name] = report.is_done
        return to_return

    @property
    def status(self) -> Status:
        if self._status in [Status.DELETED, Status.ERROR, Status.ALERT, Status.WARN, Status.SUCCESS]:
            # If the status was set to any of these values, the reports finished
            return self._status
        elif self.workers_done:
            # All the workers are done, return success/error
            for report_name, report in self.reports.items():
                if report.status != Status.SUCCESS:
                    self._status = report.status
                    return self._status
            self._status = Status.SUCCESS
            return self._status
        else:
            # At least one worker isn't done yet
            self._status = Status.WAITING
            return self._status

    @status.setter
    def status(self, _status: Status):
        self._status = _status

    def set_observables(self, links):
        """
        Add observables to current task.
        :param (list) links: list of strings
        """
        self.observables = TaskObservable.get_observables(links)

    def __str__(self):
        return f'<uuid: {self.uuid} - file: {self.file}>'
