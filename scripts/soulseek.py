import time
import uuid

import requests

from config import integer


class SoulseekClient:
    def __init__(
        self,
        base_url="http://127.0.0.1:5030",
        api_key=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

        if api_key:
            self.session.headers.update(
                {"X-API-Key": api_key}
            )

        self.session.headers.update(
            {"Content-Type": "application/json"}
        )

    def _url(self, path):
        return f"{self.base_url}{path}"

    def search(
        self,
        query,
        timeout_ms=15000,
        file_limit=10000,
        response_limit=100,
        max_retries=None,
        retry_delay=2,
    ):
        """
        Start a Soulseek search.

        slskd can temporarily return HTTP 409 when its
        search subsystem is still processing another
        request. Retry those conflicts.
        """

        if max_retries is None:
            max_retries = integer("soulseek.search_max_attempts", "SLSKD_SEARCH_MAX_ATTEMPTS", 5)

        last_response = None

        for attempt in range(
            1,
            max_retries + 1,
        ):
            search_id = str(uuid.uuid4())

            payload = {
                "id": search_id,
                "searchText": query,
                "searchTimeout": timeout_ms,
                "fileLimit": file_limit,
                "responseLimit": response_limit,
                "filterResponses": True,
                "maximumPeerQueueLength": 100,
                "minimumResponseFileCount": 1,
            }

            response = self.session.post(
                self._url("/api/v0/searches"),
                json=payload,
                timeout=30,
            )

            last_response = response

            if response.status_code == 409:
                if attempt >= max_retries:
                    response.raise_for_status()

                print(
                    "slskd returned HTTP 409 while "
                    "starting search; retrying "
                    f"({attempt}/{max_retries})...",
                    flush=True,
                )

                time.sleep(
                    retry_delay * attempt
                )

                continue

            response.raise_for_status()

            return search_id

        if last_response is not None:
            last_response.raise_for_status()

        raise RuntimeError(
            "Failed to start Soulseek search."
        )

    def get_search(
        self,
        search_id,
        include_responses=True,
        timeout=5,
    ):
        response = self.session.get(
            self._url(
                f"/api/v0/searches/{search_id}"
            ),
            params={
                "includeResponses":
                    str(include_responses).lower()
            },
            timeout=timeout,
        )

        response.raise_for_status()

        return response.json()

    def wait_for_search(
        self,
        search_id,
        timeout_seconds=30,
    ):
        # The wall-clock timeout is enforced here rather than by a single
        # HTTP request. A hung slskd request must not extend a 20/30-second
        # search wait indefinitely.
        deadline = time.monotonic() + timeout_seconds

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self.get_search(
                    search_id,
                    include_responses=True,
                    timeout=5,
                )

            request_timeout = max(
                1,
                min(5, remaining),
            )

            try:
                data = self.get_search(
                    search_id,
                    include_responses=True,
                    timeout=request_timeout,
                )
            except requests.Timeout:
                if time.monotonic() >= deadline:
                    return {
                        "id": search_id,
                        "isComplete": False,
                        "state": "TimedOut",
                        "responses": [],
                    }
                time.sleep(min(1, max(0, deadline - time.monotonic())))
                continue

            if (
                data.get("isComplete")
                or data.get("state") in {
                    "Completed",
                    "Complete",
                    "Cancelled",
                    "Errored",
                    "Failed",
                }
            ):
                return data

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return data

            time.sleep(min(1, remaining))

    def delete_search(self, search_id):
        response = self.session.delete(
            self._url(
                f"/api/v0/searches/{search_id}"
            ),
            timeout=30,
        )

        if response.status_code not in {
            200,
            204,
            404,
        }:
            response.raise_for_status()

    def enqueue_download(
        self,
        username,
        filename,
        size=None,
    ):
        """
        Queue a file for download from a Soulseek user.

        slskd expects POST
        /api/v0/transfers/downloads/{username}

        with a list of download requests.
        """

        payload = {
            "filename": filename,
        }

        if size is not None:
            payload["size"] = size

        response = self.session.post(
            self._url(
                f"/api/v0/transfers/downloads/"
                f"{username}"
            ),
            json=[payload],
            timeout=30,
        )

        response.raise_for_status()

        if response.content:
            try:
                return response.json()
            except ValueError:
                return None

        return None

    def get_downloads(self):
        """
        Get all current Soulseek downloads.
        """

        response = self.session.get(
            self._url(
                "/api/v0/transfers/downloads"
            ),
            timeout=30,
        )

        response.raise_for_status()

        return response.json()

    def get_user_downloads(
        self,
        username,
    ):
        """
        Get downloads belonging to one
        Soulseek username.
        """

        response = self.session.get(
            self._url(
                f"/api/v0/transfers/downloads/"
                f"{username}"
            ),
            timeout=30,
        )

        response.raise_for_status()

        return response.json()


def flatten_responses(search_data):
    candidates = []

    responses = search_data.get(
        "responses",
        [],
    )

    if not isinstance(
        responses,
        list,
    ):
        return candidates

    for response in responses:
        if not isinstance(
            response,
            dict,
        ):
            continue

        username = response.get(
            "username"
        )

        peer_info = {
            "username": username,
            "has_free_upload_slot":
                response.get(
                    "hasFreeUploadSlot"
                ),
            "upload_speed":
                response.get(
                    "uploadSpeed"
                ),
            "queue_length":
                response.get(
                    "queueLength"
                ),
        }

        files = response.get(
            "files",
            [],
        )

        if not isinstance(
            files,
            list,
        ):
            continue

        for file_info in files:
            if not isinstance(
                file_info,
                dict,
            ):
                continue

            candidates.append(
                {
                    "username": username,
                    "filename":
                        file_info.get(
                            "filename"
                        ),
                    "size":
                        file_info.get(
                            "size"
                        ),
                    "extension":
                        file_info.get(
                            "extension"
                        ),
                    "peer": peer_info,
                }
            )

    return candidates