# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2015-2025 CERN.
#
# Invenio is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""API for PID relations concepts."""

from __future__ import absolute_import, print_function

from invenio_db import db
from invenio_pidstore.models import PersistentIdentifier, PIDStatus
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from werkzeug.utils import cached_property

from .errors import PIDRelationConsistencyError
from .models import PIDRelation


class PIDQuery:
    """Legacy-style query wrapper for PIDNodes APIs when requesting related PIDs..

    This class provides a compatibility layer for PID-related queries that were
    originally built using the SQLAlchemy ORM's `query()` API.

    With the migration to SQLAlchemy >=2.0 and its statement-centric approach,
    this class allows older code to function without refactoring, by wrapping
    modern `select()`-based statements in an interface similar to the legacy ORM style.
    """

    def __init__(self, statement, session, _filtered_pid_class=PersistentIdentifier):
        """Constructor.

        :param statement: An initial SQLAlchemy select() statement.
        :param session: The SQLAlchemy session.
        :param _filtered_pid_class: SQLAlchemy Model class which is used for
        status filtering.
        """
        self._statement = statement
        self._session = session
        self._filtered_pid_class = _filtered_pid_class

    def ordered(self, ord="desc"):
        """Order the query result on the relations' indexes."""
        if ord not in (
            "asc",
            "desc",
        ):
            raise ValueError("Order must be 'asc' or 'desc'")
        ord_f = getattr(PIDRelation.index, ord)()
        return PIDQuery(
            self._statement.order_by(ord_f), self._session, self._filtered_pid_class
        )

    def status(self, status_in):
        """Filter the PIDs based on their status."""
        if isinstance(status_in, PIDStatus):
            status_in = [
                status_in,
            ]
        return PIDQuery(
            self._statement.where(self._filtered_pid_class.status.in_(status_in)),
            self._session,
            self._filtered_pid_class,
        )

    def filter(self, *args):
        """Apply a filter to the statement."""
        return PIDQuery(
            self._statement.filter(*args), self._session, self._filtered_pid_class
        )

    def filter_by(self, **kwargs):
        """Apply a filter by to the statement."""
        return PIDQuery(
            self._statement.filter_by(**kwargs),
            self._session,
            self._filtered_pid_class,
        )

    def join(self, *args, **kwargs):
        """Apply a join to the statement."""
        return PIDQuery(
            self._statement.join(*args, **kwargs),
            self._session,
            self._filtered_pid_class,
        )

    def count(self):
        """Count the results of the query."""
        return self._session.scalar(
            select(db.func.count()).select_from(self._statement.subquery())
        )

    def first(self):
        """Get the first result."""
        return self._session.scalars(self._statement.limit(1)).first()

    def one(self):
        """Get exactly one result."""
        return self._session.scalars(self._statement).one()

    def one_or_none(self):
        """Get one result or None if no results."""
        return self._session.scalars(self._statement).one_or_none()

    def all(self):
        """Get all results."""
        return self._session.scalars(self._statement).all()

    def exists(self):
        """Check if any results exist."""
        return self._session.scalar(
            select(1).select_from(self._statement.subquery()).exists().select()
        )


def resolve_pid(fetched_pid):
    """Retrieve the real PID given a fetched PID.

    :param pid: fetched PID to resolve.
    """
    return PersistentIdentifier.get(
        pid_type=fetched_pid.pid_type,
        pid_value=fetched_pid.pid_value,
        pid_provider=fetched_pid.provider.pid_provider,
    )


class PIDNode(object):
    """PID Node API.

    A node can have multiple parents and multiple children for a given
    relation_type.
    """

    def __init__(self, pid, relation_type, max_children=None, max_parents=None):
        """Constructor.

        :param pid: the central PID of the node.
        :param relation_type: one of the declared relation types from config.
        :param max_children: maximum number of children allowed.
        :param max_parents: maximum number of parents
            for each child of the node.
        """
        super(PIDNode, self).__init__()
        self.relation_type = relation_type
        self.pid = pid
        self.max_children = max_children
        self.max_parents = max_parents

    @cached_property
    def _resolved_pid(self):
        """Resolve self.pid if it is a fetched pid."""
        if not isinstance(self.pid, PersistentIdentifier):
            return resolve_pid(self.pid)
        return self.pid

    def _get_child_relation(self, child_pid):
        """Retrieve the relation between this node and a child PID."""
        stmt = select(PIDRelation).filter_by(
            parent=self._resolved_pid,
            child=child_pid,
            relation_type=self.relation_type.id,
        )
        return db.session.execute(stmt).scalar_one()

    def _check_child_limits(self, child_pid):
        """Check that inserting a child is within the limits."""
        if self.max_children is not None and self.children.count() >= self.max_children:
            raise PIDRelationConsistencyError(
                "Max number of children is set to {}.".format(self.max_children)
            )
        if self.max_parents is not None:
            stmt = (
                select(db.func.count())
                .select_from(PIDRelation)
                .filter_by(child=child_pid, relation_type=self.relation_type.id)
            )
            if db.session.execute(stmt).scalar() >= self.max_parents:
                raise PIDRelationConsistencyError(
                    "This pid already has the maximum number of parents."
                )

    def _connected_pids(self, from_parent=True):
        """Follow a relationship to find connected PIDs.

        :param from_parent: search children from the current pid if True, else
        search for its parents.
        :type from_parent: bool
        """
        to_pid = aliased(PersistentIdentifier, name="to_pid")
        if from_parent:
            to_relation_id = PIDRelation.child_id
            from_relation_id = PIDRelation.parent_id
        else:
            to_relation_id = PIDRelation.parent_id
            from_relation_id = PIDRelation.child_id

        initial_stmt = select(to_pid).join(
            PIDRelation,
            and_(
                to_pid.id == to_relation_id,
                PIDRelation.relation_type == self.relation_type.id,
            ),
        )

        query_builder = PIDQuery(initial_stmt, db.session(), _filtered_pid_class=to_pid)

        # Accept both PersistentIdentifier models and fake PIDs with just
        # pid_value, pid_type as they are fetched with the PID fetcher.
        if isinstance(self.pid, PersistentIdentifier):
            query_builder = query_builder.filter(from_relation_id == self.pid.id)
        else:
            from_pid = aliased(PersistentIdentifier, name="from_pid")
            query_builder = query_builder.join(
                from_pid, from_pid.id == from_relation_id
            ).filter(
                from_pid.pid_value == self.pid.pid_value,
                from_pid.pid_type == self.pid.pid_type,
            )

        return query_builder

    @property
    def parents(self):
        """Retrieves all parent PIDs."""
        return self._connected_pids(from_parent=False)

    @property
    def children(self):
        """Retrieves all child PIDs."""
        return self._connected_pids(from_parent=True)

    @property
    def is_parent(self):
        """Test if the given PID has any children."""
        return self.children.exists()

    @property
    def is_child(self):
        """Test if the given PID has any parents."""
        return self.parents.exists()

    def insert_child(self, child_pid):
        """Add the given PID to the list of children PIDs."""
        self._check_child_limits(child_pid)
        try:
            # TODO: Here add the check for the max parents and the max children
            with db.session.begin_nested():
                if not isinstance(child_pid, PersistentIdentifier):
                    child_pid = resolve_pid(child_pid)
                return PIDRelation.create(
                    self._resolved_pid, child_pid, self.relation_type.id, None
                )
        except IntegrityError:
            raise PIDRelationConsistencyError("PID Relation already exists.")

    def remove_child(self, child_pid):
        """Remove a child from a PID concept."""
        with db.session.begin_nested():
            if not isinstance(child_pid, PersistentIdentifier):
                child_pid = resolve_pid(child_pid)
            stmt = select(PIDRelation).filter_by(
                parent=self._resolved_pid,
                child=child_pid,
                relation_type=self.relation_type.id,
            )
            relation = db.session.execute(stmt).scalar_one()
            db.session.delete(relation)


class PIDNodeOrdered(PIDNode):
    """PID Node API.

    A node can have multiple parents and multiple children for a given
    relation_type.
    """

    def index(self, child_pid):
        """Index of the child in the relation."""
        if not isinstance(child_pid, PersistentIdentifier):
            child_pid = resolve_pid(child_pid)
        stmt = select(PIDRelation).filter_by(
            parent=self._resolved_pid,
            child=child_pid,
            relation_type=self.relation_type.id,
        )
        relation = db.session.execute(stmt).scalar_one()
        return relation.index

    def is_last_child(self, child_pid):
        """
        Determine if 'pid' is the latest version of a resource.

        Resolves True for Versioned PIDs which are the oldest of its siblings.
        False otherwise, also for Head PIDs.
        """
        last_child = self.last_child
        if last_child is None:
            return False
        return last_child == child_pid

    @property
    def last_child(self):
        """
        Get the latest PID as pointed by the Head PID.

        If the 'pid' is a Head PID, return the latest of its children.
        If the 'pid' is a Version PID, return the latest of its siblings.
        Return None for the non-versioned PIDs.
        """
        return self.children.filter(PIDRelation.index.isnot(None)).ordered().first()

    def next_child(self, child_pid):
        """Get the next child PID in the PID relation."""
        relation = self._get_child_relation(child_pid)
        if relation.index is not None:
            return (
                self.children.filter(PIDRelation.index > relation.index)
                .ordered(ord="asc")
                .first()
            )
        else:
            return None

    def previous_child(self, child_pid):
        """Get the previous child PID in the PID relation."""
        relation = self._get_child_relation(child_pid)
        if relation.index is not None:
            return (
                self.children.filter(PIDRelation.index < relation.index)
                .ordered(ord="desc")
                .first()
            )
        else:
            return None

    def insert_child(self, child_pid, index=-1):
        """Insert a new child into a PID concept.

        Argument 'index' can take the following values:
            0,1,2,... - insert child PID at the specified position
            -1 - insert the child PID at the last position
            None - insert child without order (no re-ordering is done)

            NOTE: If 'index' is specified, all sibling relations should
                  have PIDRelation.index information.

        """
        self._check_child_limits(child_pid)
        if index is None:
            index = -1
        try:
            with db.session.begin_nested():
                if not isinstance(child_pid, PersistentIdentifier):
                    child_pid = resolve_pid(child_pid)
                stmt = (
                    select(PIDRelation)
                    .filter(
                        PIDRelation.parent_id == self._resolved_pid.id,
                        PIDRelation.relation_type == self.relation_type.id,
                    )
                    .order_by(PIDRelation.index)
                )
                child_relations = db.session.execute(stmt).scalars().all()
                relation_obj = PIDRelation.create(
                    self._resolved_pid, child_pid, self.relation_type.id, None
                )
                if index == -1:
                    child_relations.append(relation_obj)
                else:
                    child_relations.insert(index, relation_obj)
                for idx, c in enumerate(child_relations):
                    c.index = idx
        except IntegrityError:
            raise PIDRelationConsistencyError("PID Relation already exists.")

    def remove_child(self, child_pid, reorder=False):
        """Remove a child from a PID concept."""
        super(PIDNodeOrdered, self).remove_child(child_pid)
        stmt = (
            select(PIDRelation)
            .filter(
                PIDRelation.parent_id == self._resolved_pid.id,
                PIDRelation.relation_type == self.relation_type.id,
            )
            .order_by(PIDRelation.index)
        )
        child_relations = db.session.execute(stmt).scalars().all()
        if reorder:
            for idx, c in enumerate(child_relations):
                c.index = idx


__all__ = (
    "PIDNode",
    "PIDNodeOrdered",
)
