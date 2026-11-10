# -*- coding: utf-8 -*-
__title__ = "Open in\nMassing"
__doc__ = """Open the current model's SadSha Build Pro project in the web viewer (BIM viewer, GC portal,
proforma). Uses the default project name from Settings, or the Revit file name."""
__author__ = "SadSha Build Pro (massing.build)"

import os

import massing_config as mc
from massing_api import MassingError
from pyrevit import forms, script

doc = __revit__.ActiveUIDocument.Document  # noqa: F821

client = mc.get_client()
if not client:
    script.exit()

cfg = mc.read()
project_name = cfg["project_name"] or (os.path.splitext(os.path.basename(doc.PathName))[0] if doc.PathName else "")
if not project_name:
    forms.alert("Save the Revit model first (or set a default project name in Settings).",
                title="SadSha Build Pro", warn_icon=True)
    script.exit()

try:
    pid = client.find_or_create_project(project_name)
except MassingError as e:
    forms.alert("Couldn't reach SadSha Build Pro:\n{0}".format(e), title="SadSha Build Pro", warn_icon=True)
    script.exit()

script.open_url(client.viewer_url(pid))
