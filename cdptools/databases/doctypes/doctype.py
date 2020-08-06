#!/usr/bin/env python
# -*- coding: utf-8 -*-

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict


class Doctype(ABC):
    """
    Doctype is a schema as a class.
    Source: docs/document_store_schema.md.
    """

    @staticmethod
    def from_dict(source: Dict[str, Any]) -> object:
        """
        Convert schema dict to custom object.
        """
        return object()

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert custom object to schema dict.
        """
        return {}

    def __str__(self):
        return str(self.to_dict())

    def __repr__(self):
        return self.__str__()

    def __eq__(self, a, b):
        return str(a) == str(b)
