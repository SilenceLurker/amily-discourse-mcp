"""A deliberately small API surface. Credentials never follow redirects."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .errors import RemoteError
from .store import valid_origin


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Discourse:
    def __init__(self, account: dict, vault, *, allow_http=False, timeout=20):
        self.account = account
        self.origin = valid_origin(account["site"], allow_http=allow_http)
        self.vault = vault
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method: str, path: str, payload=None) -> dict:
        write = method != "GET"
        secret = self.vault.decrypt(self.account["alias"], self.account["secret"])
        request = Request(self.origin + path, method=method,
                          data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                          headers={"Api-Key": secret, "Api-Username": self.account["username"],
                                   "Accept": "application/json", "Content-Type": "application/json",
                                   "User-Agent": "Amily-Discourse-MCP/0.1"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise RemoteError("Discourse response exceeded the size limit.", uncertain=write)
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except HTTPError as error:
            # Do not echo upstream text: it may contain submitted content or credentials.
            status = error.code
            error.close()
            raise RemoteError(f"Discourse returned HTTP {status}; response body omitted.",
                              uncertain=write and not 400 <= status < 500) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise RemoteError("Discourse request failed or returned invalid JSON; response body omitted.",
                              uncertain=write) from None
        finally:
            # Python strings cannot be reliably zeroed; only retain the key for this request.
            request.remove_header("Api-key")
            secret = None
