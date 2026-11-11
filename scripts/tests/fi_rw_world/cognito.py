"""Fake Cognito user pool (moto's needs ``joserfc``, not in the venv).

Shape source: the live CMS pool, read 2026-10-10 (``describe-user-pool``,
``list-groups``, ``describe-user-pool-client``): ``UsernameAttributes: [email]`` so a
user's ``Username`` is its ``sub`` UUID and the email is an alias; custom attributes
``custom:fleetIds`` / ``custom:dealerIds`` / ``custom:customerId`` / ...; 21 groups
including ``fleet-operator``, ``service-advisor``, ``dealer-admin``; one app client
whose ``ExplicitAuthFlows`` include ``ALLOW_USER_PASSWORD_AUTH``.

ID tokens are HMAC-signed with a world secret; ``verify`` is what the API Gateway
emulation uses, so a token this pool did not mint is refused with 401.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from typing import Any

from scripts.tests.fi_rw_world.awsfake import AwsError, Router

GROUPS = (
    "dms-viewer vehicle-owner parts-manager dispatcher connect-agent dms-technician "
    "fleet-driver agent service-advisor district-manager connected-services subscriber "
    "f-and-i-manager product-engineer dealer-admin fleet-viewer fleet-operator bdc-rep "
    "platform-admin fleet-guest"
).split()


def _b64(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).rstrip(b"=").decode()


class FakeUserPool:
    def __init__(self, clock: Any, region: str = "us-west-2") -> None:
        self.clock = clock
        self.pool_id = f"{region}_FiRwWorld"
        self.client_id = "firwworldclient0000000000"
        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{self.pool_id}"
        self._secret = secrets.token_bytes(32)
        self.users: dict[str, dict[str, Any]] = {}   # sub -> user
        self.deleted: list[str] = []

    # -- tokens ----------------------------------------------------------
    def mint(self, user: dict[str, Any]) -> str:
        now = int(self.clock.epoch())
        claims: dict[str, Any] = {
            "sub": user["sub"], "iss": self.issuer, "aud": self.client_id,
            "token_use": "id", "auth_time": now, "iat": now, "exp": now + 3600,
            "cognito:username": user["sub"], "email_verified": True,
            "jti": str(uuid.uuid4()), "origin_jti": str(uuid.uuid4()),
            "event_id": str(uuid.uuid4()),
        }
        for k, v in user["attrs"].items():
            if k == "email_verified":
                claims[k] = v == "true"
            elif k != "sub":
                claims[k] = v
        if user["groups"]:
            claims["cognito:groups"] = sorted(user["groups"])
        head = _b64(json.dumps({"alg": "HS256", "kid": "fi-rw-world"}).encode())
        body = _b64(json.dumps(claims).encode())
        sig = _b64(hmac.new(self._secret, f"{head}.{body}".encode(), hashlib.sha256).digest())
        return f"{head}.{body}.{sig}"

    def verify(self, token: str) -> dict[str, Any] | None:
        try:
            head, body, sig = token.split(".")
        except ValueError:
            return None
        want = _b64(hmac.new(self._secret, f"{head}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(want, sig):
            return None
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if claims.get("token_use") != "id" or claims.get("iss") != self.issuer:
            return None
        if claims.get("exp", 0) < self.clock.epoch():
            return None
        if claims.get("sub") not in self.users:
            return None
        return claims

    # -- lookup ----------------------------------------------------------
    def _find(self, username: str) -> dict[str, Any]:
        for u in self.users.values():
            if username in (u["sub"], u["attrs"].get("email")):
                return u
        raise AwsError("UserNotFoundException", "User does not exist.")

    def _check_pool(self, p: dict[str, Any]) -> None:
        if p.get("UserPoolId") != self.pool_id:
            raise AwsError("ResourceNotFoundException", f"User pool {p.get('UserPoolId')} does not exist.")

    @staticmethod
    def _user_type(u: dict[str, Any]) -> dict[str, Any]:
        attrs = [{"Name": "sub", "Value": u["sub"]}] + [
            {"Name": k, "Value": v} for k, v in u["attrs"].items()]
        return {"Username": u["sub"], "Attributes": attrs, "Enabled": True,
                "UserStatus": u["status"], "UserCreateDate": u["created"],
                "UserLastModifiedDate": u["created"]}

    # -- operations -------------------------------------------------------
    def list_user_pool_clients(self, p: dict[str, Any]) -> dict[str, Any]:
        self._check_pool(p)
        return {"UserPoolClients": [{"ClientId": self.client_id, "UserPoolId": self.pool_id,
                                     "ClientName": "CMSUserPoolClientWorld"}]}

    def describe_user_pool_client(self, p: dict[str, Any]) -> dict[str, Any]:
        self._check_pool(p)
        if p.get("ClientId") != self.client_id:
            raise AwsError("ResourceNotFoundException", "client not found")
        return {"UserPoolClient": {
            "UserPoolId": self.pool_id, "ClientId": self.client_id,
            "ClientName": "CMSUserPoolClientWorld",
            "ExplicitAuthFlows": ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_PASSWORD_AUTH",
                                  "ALLOW_USER_SRP_AUTH"]}}

    def admin_create_user(self, p: dict[str, Any]) -> dict[str, Any]:
        self._check_pool(p)
        email = p["Username"]
        # C3 fix: pool uses UsernameAttributes=[email]; reject non-email-format usernames.
        if "@" not in email or "." not in email.split("@")[-1]:
            raise AwsError(
                "InvalidParameterException",
                f"Username must be an email address (pool uses UsernameAttributes=[email]); "
                f"got {email!r}",
            )
        if any(u["attrs"].get("email") == email for u in self.users.values()):
            raise AwsError("UsernameExistsException", "An account with the given email already exists.")
        attrs = {a["Name"]: a["Value"] for a in p.get("UserAttributes") or []}
        attrs.setdefault("email", email)
        sub = str(uuid.uuid4())
        u = {"sub": sub, "attrs": attrs, "groups": set(), "password": None,
             "status": "FORCE_CHANGE_PASSWORD", "created": self.clock.now()}
        self.users[sub] = u
        return {"User": self._user_type(u)}

    def admin_set_user_password(self, p: dict[str, Any]) -> dict[str, Any]:
        self._check_pool(p)
        u = self._find(p["Username"])
        u["password"] = p["Password"]
        if p.get("Permanent"):
            u["status"] = "CONFIRMED"
        return {}

    def admin_add_user_to_group(self, p: dict[str, Any]) -> dict[str, Any]:
        self._check_pool(p)
        if p["GroupName"] not in GROUPS:
            raise AwsError("ResourceNotFoundException", "Group not found.")
        self._find(p["Username"])["groups"].add(p["GroupName"])
        return {}

    def initiate_auth(self, p: dict[str, Any]) -> dict[str, Any]:
        if p.get("ClientId") != self.client_id:
            raise AwsError("ResourceNotFoundException", "client not found")
        if p.get("AuthFlow") != "USER_PASSWORD_AUTH":
            raise AwsError("InvalidParameterException", "flow not enabled")
        ap = p.get("AuthParameters") or {}
        try:
            u = self._find(ap.get("USERNAME", ""))
        except AwsError:
            raise AwsError("NotAuthorizedException", "Incorrect username or password.")
        if u["password"] is None or ap.get("PASSWORD") != u["password"] or u["status"] != "CONFIRMED":
            raise AwsError("NotAuthorizedException", "Incorrect username or password.")
        return {"AuthenticationResult": {"IdToken": self.mint(u), "AccessToken": "world-access",
                                         "RefreshToken": "world-refresh", "ExpiresIn": 3600,
                                         "TokenType": "Bearer"}, "ChallengeParameters": {}}

    def admin_delete_user(self, p: dict[str, Any]) -> dict[str, Any]:
        self._check_pool(p)
        u = self._find(p["Username"])
        del self.users[u["sub"]]
        self.deleted.append(u["sub"])
        return {}

    def admin_get_user(self, p: dict[str, Any]) -> dict[str, Any]:
        self._check_pool(p)
        t = self._user_type(self._find(p["Username"]))
        return {"Username": t["Username"], "UserAttributes": t["Attributes"],
                "Enabled": True, "UserStatus": t["UserStatus"],
                "UserCreateDate": t["UserCreateDate"], "UserLastModifiedDate": t["UserCreateDate"]}

    def list_users(self, p: dict[str, Any]) -> dict[str, Any]:
        self._check_pool(p)
        users = sorted(self.users.values(), key=lambda u: u["sub"])
        flt = (p.get("Filter") or "").strip()
        if flt:
            m = re.fullmatch(r'"?([\w:]+)"?\s*(\^=|=)\s*"([^"]*)"', flt)
            if not m:
                raise AwsError("InvalidParameterException", f"bad filter {flt!r}")
            name, op, val = m.groups()

            def get(u: dict[str, Any]) -> str:
                return u["sub"] if name in ("sub", "username") else u["attrs"].get(name, "")
            users = [u for u in users if (get(u).startswith(val) if op == "^=" else get(u) == val)]
        limit = int(p.get("Limit") or 60)
        start = int(p.get("PaginationToken") or 0)
        page = users[start:start + limit]
        out: dict[str, Any] = {"Users": [self._user_type(u) for u in page]}
        if start + limit < len(users):
            out["PaginationToken"] = str(start + limit)
        return out

    def list_user_pools(self, p: dict[str, Any]) -> dict[str, Any]:
        """Return a single-page list containing this pool.

        The pool name is ``cms-{stage}-ui-users`` — the name the live CMS
        Cognito pool carries and that ``_resolve_cms_pool_id`` searches for.
        Source: live ``aws cognito-idp list-user-pools`` output (2026-10-10).
        """
        # Derive stage from pool_id suffix: "{region}_FiRwWorld" → "staging".
        # Real pool name for staging: cms-staging-ui-users.
        # _FiRwWorld suffix makes "staging" the default; any override world that
        # uses a different pool_id should override this method too.
        pool_name = "cms-staging-ui-users"
        return {
            "UserPools": [
                {"Id": self.pool_id, "Name": pool_name,
                 "LambdaConfig": {}, "Status": "Enabled",
                 "LastModifiedDate": self.clock.epoch(),
                 "CreationDate": self.clock.epoch()}
            ]
        }

    def register(self, router: Router) -> None:
        for op, fn in {
            "ListUserPools": self.list_user_pools,
            "ListUserPoolClients": self.list_user_pool_clients,
            "DescribeUserPoolClient": self.describe_user_pool_client,
            "AdminCreateUser": self.admin_create_user,
            "AdminSetUserPassword": self.admin_set_user_password,
            "AdminAddUserToGroup": self.admin_add_user_to_group,
            "InitiateAuth": self.initiate_auth,
            "AdminDeleteUser": self.admin_delete_user,
            "AdminGetUser": self.admin_get_user,
            "ListUsers": self.list_users,
        }.items():
            router.add("cognito-idp", op, fn)
