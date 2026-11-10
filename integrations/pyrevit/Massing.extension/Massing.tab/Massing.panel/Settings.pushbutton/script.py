# -*- coding: utf-8 -*-
__title__ = "Settings"
__doc__ = """Configure the SadSha Build Pro connection: API URL, web (app) URL and your API key.
Get a key + URLs from Settings -> SadSha Build Pro licence in the SadSha Build Pro web app (Commercial plan and up
includes REST API access). Stored per-user in pyRevit config."""
__author__ = "SadSha Build Pro (massing.build)"

import massing_config as mc
from pyrevit import forms, script

cur = mc.read()

# Simple sequential prompts (works across pyRevit versions)
api_url = forms.ask_for_string(default=cur["api_url"] or "https://massing.build/api",
                               prompt="SadSha Build Pro API URL (e.g. https://your-host/api)", title="SadSha Build Pro — API URL")
if api_url is None:
    script.exit()
app_url = forms.ask_for_string(default=cur["app_url"] or "https://massing.build",
                               prompt="SadSha Build Pro web app URL (for 'Open in SadSha Build Pro' links)", title="SadSha Build Pro — App URL")
if app_url is None:
    script.exit()
api_key = forms.ask_for_string(default=cur["api_key"],
                               prompt="API key (Bearer). Commercial plan and up.", title="SadSha Build Pro — API key")
if api_key is None:
    script.exit()
project_name = forms.ask_for_string(default=cur["project_name"] or "",
                                    prompt="Default SadSha Build Pro project name (blank = use the Revit file name)",
                                    title="SadSha Build Pro — Project")
if project_name is None:
    script.exit()

mc.write(api_url=api_url, app_url=app_url, api_key=api_key, project_name=project_name)
forms.alert("SadSha Build Pro connection saved.\nAPI: {0}".format(api_url.rstrip('/')), title="SadSha Build Pro", warn_icon=False)
