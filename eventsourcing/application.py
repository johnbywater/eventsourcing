from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, TypeVar, Union
from uuid import UUID

from eventsourcing.domain import Aggregate, Snapshot
from eventsourcing.persistence import (
    AbstractTranscoder,
    ApplicationRecorder,
    DatetimeAsISO,
    DecimalAsStr,
    EventStore,
    InfrastructureFactory,
    Mapper,
    Notification,
    Transcoder,
    UUIDAsHex,
)


class Repository:
    """Reconstructs aggregates from events in an
    :class:`eventsourcing.persistence.EventStore`,
    possibly using snapshot store to avoid replaying
    all events."""
    def __init__(
        self,
        event_store: EventStore[Aggregate.Event],
        snapshot_store: Optional[EventStore[Snapshot]] = None,
    ):
        """
        Initialises repository with given event store (an
        :class:`~eventsourcing.persistence.EventStore` for aggregate
        :class:`~eventsourcing.domain.Aggregate.Event` objects)
        and optionally a snapshot store (an
        :class:`~eventsourcing.persistence.EventStore` for aggregate
        :class:`~eventsourcing.domain.Snapshot` objects).
        """
        self.event_store = event_store
        self.snapshot_store = snapshot_store

    def get(self, aggregate_id: UUID, version: Optional[int] = None) -> Aggregate:
        """
        Returns an :class:`eventsourcing.domain.Aggregate`
        for given ID, optionally at the given version.
        """

        gt = None
        domain_events: List[Union[Snapshot, Aggregate.Event]] = []

        # Try to get a snapshot.
        if self.snapshot_store is not None:
            snapshots = self.snapshot_store.get(
                originator_id=aggregate_id,
                desc=True,
                limit=1,
                lte=version,
            )
            try:
                snapshot = next(snapshots)
                gt = snapshot.originator_version
                domain_events.append(snapshot)
            except StopIteration:
                pass

        # Get the domain events.
        domain_events += self.event_store.get(
            originator_id=aggregate_id,
            gt=gt,
            lte=version,
        )

        # Project the domain events.
        aggregate = None
        for domain_event in domain_events:
            aggregate = domain_event.mutate(aggregate)

        # Raise exception if not found.
        if aggregate is None:
            raise AggregateNotFound((aggregate_id, version))

        # Return the aggregate.
        assert isinstance(aggregate, Aggregate)
        return aggregate


@dataclass(frozen=True)
class Section:
    """
    Frozen dataclass that represents a section from a :class:`NotificationLog`.
    The :data:`items` attribute contains a list of
    :class:`eventsourcing.persistence.Notification` objects.
    The :data:`id` attribute is the section ID, two integers
    separated by a comma that described the first and last
    notification ID that are included in the section.
    The :data:`next_id` attribute describes the section ID
    of the next section, and will be set if the section contains
    as many notifications are were requested.

    Constructor arguments:

    :param str id: section ID e.g. "1,10"
    :param List[Notification] items: a list of event notifications
    :param str next_id: section ID of the next section in a notification log
    """
    id: str
    items: List[Notification]
    next_id: Optional[str]


class NotificationLog(ABC):
    """
    Abstract base class for application notification logs.
    """
    @abstractmethod
    def __getitem__(self, section_id: str) -> Section:
        """
        Returns :class:`Section` from a notification log.
        """


class LocalNotificationLog(NotificationLog):
    """
    Notification log that presents sections of event notifications
    retrieved from an :class:`~eventsourcing.persistence.ApplicationRecorder`.
    """
    DEFAULT_SECTION_SIZE = 10

    def __init__(
        self,
        recorder: ApplicationRecorder,
        section_size: int = DEFAULT_SECTION_SIZE,
    ):
        """
        Initialises a local notification object with given
        :class:`~eventsourcing.persistence.ApplicationRecorder`
        and an optional section size.

        Constructor arguments:

        :param ApplicationRecorder recorder: application recorder from which event
            notifications will be selected
        :param List[Notification] items: a list of event notifications
        :param str next_id: section ID of the next section in a notification log

        """
        self.recorder = recorder
        self.section_size = section_size

    def __getitem__(self, section_id: str) -> Section:
        """
        Returns a :class:`Section` of event notifications
        based on the requested section ID. The section ID of
        the returned section will describe the event
        notifications that are actually contained in
        the returned section, and may vary from the
        requested section ID if there are less notifications
        in the recorder than were requested, or if there
        are gaps in the sequence of recorded event notification.
        """
        # Interpret the section ID.
        parts = section_id.split(",")
        part1 = int(parts[0])
        part2 = int(parts[1])
        start = max(1, part1)
        limit = min(max(0, part2 - start + 1), self.section_size)

        # Select notifications.
        notifications = self.recorder.select_notifications(start, limit)

        # Get next section ID.
        if len(notifications):
            last_id = notifications[-1].id
            return_id = self.format_section_id(notifications[0].id, last_id)
            if len(notifications) == limit:
                next_start = last_id + 1
                next_id = self.format_section_id(next_start, next_start + limit - 1)
            else:
                next_id = None
        else:
            return_id = None
            next_id = None

        # Return a section of the notification log.
        return Section(
            id=return_id,
            items=notifications,
            next_id=next_id,
        )

    @staticmethod
    def format_section_id(first, limit):
        return "{},{}".format(first, limit)


class Application(ABC):
    """
    Base class for event-sourced applications.
    """
    def __init__(self):
        """
        Initialises an application with an
        :class:`~eventsourcing.persistence.InfrastructureFactory`,
        a :class:`~eventsourcing.persistence.Mapper`,
        an :class:`~eventsourcing.persistence.ApplicationRecorder`,
        an :class:`~eventsourcing.persistence.EventStore`,
        a :class:`~eventsourcing.application.Repository`,
        a :class:`~eventsourcing.application.LocalNotificationLog`.
        """
        self.factory = self.construct_factory()
        self.mapper = self.construct_mapper()
        self.recorder = self.construct_recorder()
        self.events = self.construct_event_store()
        self.snapshots = self.construct_snapshot_store()
        self.repository = self.construct_repository()
        self.log = self.construct_notification_log()

    def construct_factory(self) -> InfrastructureFactory:
        """
        Constructs an :class:`~eventsourcing.persistence.InfrastructureFactory`
        for use by the application.
        """
        return InfrastructureFactory.construct(self.__class__.__name__)

    def construct_mapper(self, application_name="") -> Mapper:
        """
        Constructs a :class:`~eventsourcing.persistence.Mapper`
        for use by the application.
        """
        return self.factory.mapper(
            transcoder=self.construct_transcoder(),
            application_name=application_name,
        )

    def construct_transcoder(self) -> AbstractTranscoder:
        """
        Constructs a :class:`~eventsourcing.persistence.Transcoder`
        for use by the application.
        """
        transcoder = Transcoder()
        self.register_transcodings(transcoder)
        return transcoder

    def register_transcodings(self, transcoder: Transcoder):
        """
        Registers :class:`~eventsourcing.persistence.Transcoding`
        objects on given :class:`~eventsourcing.persistence.Transcoder`.
        """
        transcoder.register(UUIDAsHex())
        transcoder.register(DecimalAsStr())
        transcoder.register(DatetimeAsISO())

    def construct_recorder(self) -> ApplicationRecorder:
        """
        Constructs an :class:`~eventsourcing.persistence.ApplicationRecorder`
        for use by the application.
        """
        return self.factory.application_recorder()

    def construct_event_store(
        self,
    ) -> EventStore[Aggregate.Event]:
        """
        Constructs an :class:`~eventsourcing.persistence.EventStore`
        for use by the application to store and retrieve aggregate
        :class:`~eventsourcing.domain.Aggregate.Event` objects.
        """
        return self.factory.event_store(
            mapper=self.mapper,
            recorder=self.recorder,
        )

    def construct_snapshot_store(
        self,
    ) -> Optional[EventStore[Snapshot]]:
        """
        Constructs an :class:`~eventsourcing.persistence.EventStore`
        for use by the application to store and retrieve aggregate
        :class:`~eventsourcing.domain.Snapshot` objects.
        """
        if not self.factory.is_snapshotting_enabled():
            return None
        recorder = self.factory.aggregate_recorder(purpose="snapshots")
        return self.factory.event_store(
            mapper=self.mapper,
            recorder=recorder,
        )

    def construct_repository(self) -> Repository:
        """
        Constructs a :class:`Repository` for use by the application.
        """
        return Repository(
            event_store=self.events,
            snapshot_store=self.snapshots,
        )

    def construct_notification_log(self) -> LocalNotificationLog:
        """
        Constructs a :class:`LocalNotificationLog` for use by the application.
        """
        return LocalNotificationLog(self.recorder, section_size=10)

    def save(self, *aggregates: Aggregate) -> None:
        """
        Collects pending events from given aggregates and
        puts them in the application's event store.
        """
        events = []
        for aggregate in aggregates:
            events += aggregate._collect_()
        self.events.put(events)
        self.notify(events)

    def notify(self, new_events: List[Aggregate.Event]):
        """
        Called after new domain events have been saved. This
        method on this class class doesn't actually do anything,
        but this method may be implemented by subclasses that
        need to take action when new domain events have been saved.
        """

    def take_snapshot(self, aggregate_id: UUID, version: Optional[int] = None):
        """
        Takes a snapshot of the recorded state of the aggregate,
        and puts the snapshot in the snapshot store.
        """
        aggregate = self.repository.get(aggregate_id, version)
        snapshot = Snapshot.take(aggregate)
        self.snapshots.put([snapshot])


TApplication = TypeVar("TApplication", bound=Application)


class AggregateNotFound(Exception):
    """
    Raised when an :class:`~eventsourcing.domain.Aggregate`
    object is not found in a :class:`Repository`.
    """