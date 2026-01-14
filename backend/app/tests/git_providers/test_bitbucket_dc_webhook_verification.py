import hashlib
import hmac
from unittest.mock import MagicMock, patch

import pytest

from app.git_providers.providers.bitbucket_dc_provider import BitbucketDCProvider


@pytest.fixture
def provider() -> BitbucketDCProvider:
    mock_config = MagicMock()
    mock_secrets_manager = MagicMock()
    return BitbucketDCProvider(mock_config, mock_secrets_manager)


class TestBitbucketDCWebhookSignatureVerification:
    """Tests for HMAC-SHA256 webhook signature verification"""

    def setup_method(self) -> None:
        self.secret_token = "test-webhook-secret-123"
        self.raw_body = b'{"eventKey": "repo:refs_changed", "repository": {}}'

        # Calculate valid signature
        expected_hash = hmac.new(
            self.secret_token.encode("utf-8"),
            msg=self.raw_body,
            digestmod=hashlib.sha256,
        ).hexdigest()
        self.valid_signature = f"sha256={expected_hash}"

    def test_valid_signature_returns_true(self, provider: BitbucketDCProvider) -> None:
        result = provider._verify_webhook_signature(
            self.raw_body, self.secret_token, self.valid_signature
        )
        assert result is True

    def test_invalid_signature_returns_false(
        self, provider: BitbucketDCProvider
    ) -> None:
        result = provider._verify_webhook_signature(
            self.raw_body, self.secret_token, "sha256=invalid_hex_signature"
        )
        assert result is False

    def test_missing_signature_header_returns_false(
        self, provider: BitbucketDCProvider
    ) -> None:
        result = provider._verify_webhook_signature(
            self.raw_body, self.secret_token, None
        )
        assert result is False

    def test_wrong_prefix_returns_false(self, provider: BitbucketDCProvider) -> None:
        result = provider._verify_webhook_signature(
            self.raw_body, self.secret_token, "md5=wrong_format"
        )
        assert result is False

    def test_empty_signature_returns_false(self, provider: BitbucketDCProvider) -> None:
        result = provider._verify_webhook_signature(
            self.raw_body, self.secret_token, ""
        )
        assert result is False

    def test_tampered_body_returns_false(self, provider: BitbucketDCProvider) -> None:
        tampered_body = b'{"eventKey": "repo:refs_changed", "tampered": true}'
        result = provider._verify_webhook_signature(
            tampered_body, self.secret_token, self.valid_signature
        )
        assert result is False

    def test_wrong_secret_returns_false(self, provider: BitbucketDCProvider) -> None:
        result = provider._verify_webhook_signature(
            self.raw_body, "wrong-secret", self.valid_signature
        )
        assert result is False


class TestBitbucketDCWebhookEventHandling:
    """Integration tests for webhook event handling with signature verification"""

    def _create_mock_context(self, raw_body: bytes | None) -> MagicMock:
        mock_ctx = MagicMock()
        mock_ctx.installation_id = "test-install-id"
        mock_ctx.organization_id = "test-org-id"
        mock_ctx.raw_body = raw_body
        return mock_ctx

    @patch.object(BitbucketDCProvider, "fetch_secrets_by_id")
    def test_handle_webhook_rejects_invalid_signature(
        self, mock_fetch_secrets: MagicMock, provider: BitbucketDCProvider
    ) -> None:
        mock_fetch_secrets.return_value = {"secret_token": "real-secret"}
        mock_ctx = self._create_mock_context(raw_body=b'{"test": true}')

        with pytest.raises(PermissionError, match="Invalid webhook signature"):
            provider.handle_webhook_event(
                headers={"x-hub-signature": "sha256=invalid"},
                payload={},
                webhook_event_ctx=mock_ctx,
            )

    @patch.object(BitbucketDCProvider, "fetch_secrets_by_id")
    def test_handle_webhook_rejects_missing_signature(
        self, mock_fetch_secrets: MagicMock, provider: BitbucketDCProvider
    ) -> None:
        mock_fetch_secrets.return_value = {"secret_token": "real-secret"}
        mock_ctx = self._create_mock_context(raw_body=b'{"test": true}')

        with pytest.raises(PermissionError, match="Invalid webhook signature"):
            provider.handle_webhook_event(
                headers={},
                payload={},
                webhook_event_ctx=mock_ctx,
            )

    @patch.object(BitbucketDCProvider, "fetch_secrets_by_id")
    def test_handle_webhook_rejects_missing_raw_body(
        self, mock_fetch_secrets: MagicMock, provider: BitbucketDCProvider
    ) -> None:
        mock_fetch_secrets.return_value = {"secret_token": "real-secret"}
        mock_ctx = self._create_mock_context(raw_body=None)

        with pytest.raises(PermissionError, match="Unable to verify webhook signature"):
            provider.handle_webhook_event(
                headers={"x-hub-signature": "sha256=test"},
                payload={},
                webhook_event_ctx=mock_ctx,
            )

    @patch.object(BitbucketDCProvider, "fetch_secrets_by_id")
    def test_handle_webhook_rejects_missing_secret_token(
        self, mock_fetch_secrets: MagicMock, provider: BitbucketDCProvider
    ) -> None:
        mock_fetch_secrets.return_value = {}  # No secret_token
        mock_ctx = self._create_mock_context(raw_body=b'{"test": true}')

        with pytest.raises(PermissionError, match="Webhook secret not configured"):
            provider.handle_webhook_event(
                headers={"x-hub-signature": "sha256=test"},
                payload={},
                webhook_event_ctx=mock_ctx,
            )

    @patch.object(BitbucketDCProvider, "fetch_secrets_by_id")
    def test_handle_webhook_accepts_valid_signature(
        self, mock_fetch_secrets: MagicMock, provider: BitbucketDCProvider
    ) -> None:
        secret_token = "test-secret"
        raw_body = b'{"eventKey": "repo:refs_changed"}'

        # Calculate valid signature
        expected_hash = hmac.new(
            secret_token.encode("utf-8"),
            msg=raw_body,
            digestmod=hashlib.sha256,
        ).hexdigest()
        valid_signature = f"sha256={expected_hash}"

        mock_fetch_secrets.return_value = {"secret_token": secret_token}
        mock_ctx = self._create_mock_context(raw_body=raw_body)

        # Should not raise, just return ignored message for unknown event
        result = provider.handle_webhook_event(
            headers={
                "x-hub-signature": valid_signature,
                "x-event-key": "unknown:event",
            },
            payload={},
            webhook_event_ctx=mock_ctx,
        )

        assert result == {"message": "Event ignored"}
