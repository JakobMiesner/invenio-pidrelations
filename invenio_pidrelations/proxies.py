# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2017-2019 CERN.
#
# Invenio is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Proxy objects for easier access to application objects."""

from flask import current_app
from werkzeug.local import LocalProxy

current_pidrelations = LocalProxy(
    lambda: current_app.extensions["invenio-pidrelations"]
)
