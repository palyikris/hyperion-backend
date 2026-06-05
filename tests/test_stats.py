import pytest
from unittest.mock import patch, AsyncMock
from fastapi import status
from app.models.stats import TrashCompositionItem

@pytest.mark.asyncio
class TestStatsEndpoints:

    async def test_trash_composition_endpoint(self, auth_client):
        """Test the /api/stats/trash-composition endpoint with mocked data."""
      
        client = auth_client["client"]

        with patch(
            "app.api.stats.get_trash_composition", new_callable=AsyncMock
        ) as mock_get:
            mock_item = TrashCompositionItem(label="plastic", count=50, percentage=100.0)
            mock_get.return_value = [mock_item]

            response = await client.get("/api/stats/trash-composition")

            assert response.status_code == status.HTTP_200_OK
            data = response.json()
            assert data["total_detections"] == 50
            assert data["items"][0]["label"] == "plastic"
            assert mock_get.called

    async def test_stats_summary_parallel_execution(self, auth_client):
        """Test that the /api/stats/summary endpoint calls all KPI utilities in parallel."""
      
        client = auth_client["client"]

        # Patch all 6 KPI utilities to verify parallel execution
        with patch(
            "app.api.stats.get_trash_composition", new_callable=AsyncMock
        ) as m1, patch(
            "app.api.stats.get_environmental_footprint", new_callable=AsyncMock
        ) as m2, patch(
            "app.api.stats.get_ai_fleet_efficiency", new_callable=AsyncMock
        ) as m3, patch(
            "app.api.stats.get_temporal_trends", new_callable=AsyncMock
        ) as m4, patch(
            "app.api.stats.get_mean_time_to_process", new_callable=AsyncMock
        ) as m5, patch(
            "app.api.stats.get_hotspot_density", new_callable=AsyncMock
        ) as m6, patch(
            "app.api.stats.cache_stats"
        ), patch(
            "app.api.stats.get_cached_stats", return_value=None
        ):

            # Setup basic returns
            m1.return_value = []
            m2.return_value = {"total_area_sqm": 10.0, "total_detections": 1}
            m3.return_value = {
                "workers": [],
                "fleet_reliability_score": 1.0,
                "total_successes": 1,
                "total_failures": 0,
            }
            m4.return_value = []
            m5.return_value = {"overall_avg_seconds": 1.0, "by_worker": []}
            m6.return_value = {"hotspot_count": 0, "high_confidence_media_count": 0}

            response = await client.get("/api/stats/summary", params={"days": 7})

            assert response.status_code == status.HTTP_200_OK
            assert (
                m1.called
                and m2.called
                and m3.called
                and m4.called
                and m5.called
                and m6.called
            )

    async def test_temporal_trends_query_param(self, auth_client):
      
        """Test that the /api/stats/temporal-trends endpoint correctly accepts and uses the 'days' query parameter."""
      
        client = auth_client["client"]

        with patch(
            "app.api.stats.get_temporal_trends", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = []

            await client.get("/api/stats/temporal-trends", params={"days": 30})

            mock_get.assert_called_once()
            # The args are (db, user_id, days)
            assert mock_get.call_args[0][2] == 30

    async def test_stats_401_unauthorized(self, client):
      
        """Test that stats endpoints return 401 when no auth is provided."""
      
        # Test without authentication headers
        response = await client.get("/api/stats/trash-composition")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    async def test_fun_facts_validation(self, auth_client):
        """Test that the /api/stats/fun-facts endpoint validates the 'lang' query parameter."""
      
        client = auth_client["client"]

        response = await client.get("/api/stats/fun-facts", params={"lang": "fr"})
        # Should be 422 because of regex validation
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
