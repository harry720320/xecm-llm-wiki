"""Unit tests for XecmSourceDocumentService."""
import pytest
from services.xecm_source import XecmServiceFactory, XecmSourceDocumentService


def test_xecm_service_factory_creates_services():
    factory = XecmServiceFactory(db=None, storage=None, user_id="test")
    doc_svc = factory.document_service("test")
    assert isinstance(doc_svc, XecmSourceDocumentService)
