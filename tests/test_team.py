import unittest

from harness import PASSWORD, Client, TestServer


class Invites(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.owner = Client(cls.ts)
        assert cls.owner.register("Team Repair", "team-owner@example.com").status == 200

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def invite(self, email, role="technician"):
        r = self.owner.post("/api/invites", {"email": email, "role": role})
        self.assertEqual(r.status, 200, r.raw)
        return r.json

    def join(self, token, name="New Hire", password=PASSWORD):
        c = Client(self.ts)
        r = c.post("/api/invites/accept", {"token": token, "name": name, "password": password})
        if r.status == 200:
            c.csrf = r.json["csrf"]
        return c, r

    def test_full_invite_flow(self):
        inv = self.invite("hire1@example.com", "technician")
        c = Client(self.ts)
        prev = c.post("/api/invites/preview", {"token": inv["token"]})
        self.assertEqual(prev.status, 200)
        self.assertEqual(prev.json["email"], "hire1@example.com")
        self.assertEqual(prev.json["business_name"], "Team Repair")
        self.assertEqual(prev.json["role_label"], "Technician")

        hire, r = self.join(inv["token"])
        self.assertEqual(r.status, 200)
        me = hire.get("/api/me").json
        self.assertEqual(me["user"]["role"], "technician")
        self.assertEqual(me["business"]["name"], "Team Repair")
        self.assertEqual(Client(self.ts).login("hire1@example.com").status, 200)

        _, again = self.join(inv["token"])             # single use
        self.assertEqual(again.status, 400)
        pending = [i["email"] for i in self.owner.get("/api/invites").json["invites"]]
        self.assertNotIn("hire1@example.com", pending)

    def test_token_not_stored_in_clear(self):
        inv = self.invite("hash-invite@example.com")
        secret = inv["token"].split(".")[1]
        db = self.ts.tenant_db(self.owner.tenant_id)
        hashes = [r[0] for r in db.execute("SELECT token_hash FROM invites")]
        db.close()
        self.assertNotIn(secret, hashes)

    def test_expired_invite_rejected(self):
        inv = self.invite("late@example.com")
        db = self.ts.tenant_db(self.owner.tenant_id)
        db.execute("UPDATE invites SET expires_at = 1 WHERE email='late@example.com'")
        db.close()
        self.assertEqual(Client(self.ts).post("/api/invites/preview", {"token": inv["token"]}).status, 400)
        self.assertEqual(self.join(inv["token"])[1].status, 400)

    def test_revoked_invite_rejected_and_reinvite_revokes_old(self):
        inv = self.invite("revoke@example.com")
        invite_id = inv["invite"]["id"]
        self.assertEqual(self.owner.request("DELETE", "/api/invites/%s" % invite_id).status, 200)
        self.assertEqual(self.join(inv["token"])[1].status, 400)
        first = self.invite("again@example.com")
        second = self.invite("again@example.com")
        self.assertEqual(self.join(first["token"])[1].status, 400)
        self.assertEqual(self.join(second["token"])[1].status, 200)

    def test_garbage_tokens(self):
        for bad in (None, 5, "", "nodot", "a.b.c", "0" * 16 + ".", "zz.zz", "0" * 16 + ".wrong"):
            r = Client(self.ts).post("/api/invites/preview", {"token": bad})
            self.assertEqual(r.status, 400, repr(bad))

    def test_validation(self):
        self.assertEqual(self.owner.post("/api/invites", {"email": "x@example.com", "role": "owner"}).status, 400)
        self.assertEqual(self.owner.post("/api/invites", {"email": "x@example.com", "role": "nope"}).status, 400)
        self.assertEqual(self.owner.post("/api/invites", {"email": "bad", "role": "manager"}).status, 400)
        self.assertEqual(self.owner.post("/api/invites",
                                         {"email": "team-owner@example.com", "role": "manager"}).status, 409)
        inv = self.invite("weakpw@example.com")
        self.assertEqual(self.join(inv["token"], password="short")[1].status, 400)

    def test_non_managers_cannot_invite(self):
        self.ts.add_user(self.owner.tenant_id, "plain-tech@example.com", "technician")
        tech = Client(self.ts)
        tech.login("plain-tech@example.com")
        self.assertEqual(tech.post("/api/invites", {"email": "y@example.com", "role": "technician"}).status, 403)
        self.assertEqual(tech.get("/api/invites").status, 403)

    def test_role_change_and_deactivate(self):
        inv = self.invite("mutable@example.com", "technician")
        hire, _ = self.join(inv["token"])
        uid = hire.get("/api/me").json["user"]["id"]

        r = self.owner.put("/api/users/%s" % uid, {"role": "front_desk"})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json["role"], "front_desk")
        self.assertEqual(hire.get("/api/me").json["user"]["role"], "front_desk")

        self.assertEqual(self.owner.put("/api/users/%s" % uid, {"role": "owner"}).status, 400)
        self.assertEqual(self.owner.put("/api/users/%s" % uid, {"active": "no"}).status, 400)
        self.assertEqual(self.owner.put("/api/users/%s" % uid, {}).status, 400)

        self.assertEqual(self.owner.put("/api/users/%s" % uid, {"active": False}).status, 200)
        self.assertEqual(hire.get("/api/me").status, 401)                      # sessions killed
        self.assertEqual(Client(self.ts).login("mutable@example.com").status, 401)
        self.assertEqual(self.owner.put("/api/users/%s" % uid, {"active": True}).status, 200)
        self.assertEqual(Client(self.ts).login("mutable@example.com").status, 200)

    def test_owner_and_self_are_protected(self):
        owner_id = self.owner.get("/api/me").json["user"]["id"]
        self.assertEqual(self.owner.put("/api/users/%s" % owner_id, {"active": False}).status, 400)
        self.assertEqual(self.owner.put("/api/users/%s" % owner_id, {"role": "manager"}).status, 400)

    def test_changes_are_audited(self):
        self.invite("audited@example.com")
        actions = [e["action"] for e in self.owner.get("/api/audit").json["events"]]
        self.assertIn("invite.created", actions)

    def test_roles_listing_excludes_owner(self):
        names = [r["name"] for r in self.owner.get("/api/roles").json["roles"]]
        self.assertEqual(sorted(names), ["front_desk", "manager", "technician"])


class InviteIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = TestServer()
        cls.a, cls.b = Client(cls.ts), Client(cls.ts)
        assert cls.a.register("Alpha Team", "a-owner@example.com").status == 200
        assert cls.b.register("Beta Team", "b-owner@example.com").status == 200

    @classmethod
    def tearDownClass(cls):
        cls.ts.close()

    def test_cannot_revoke_other_tenants_invite(self):
        inv = self.a.post("/api/invites", {"email": "h@example.com", "role": "manager"}).json
        self.assertEqual(self.b.request("DELETE", "/api/invites/%s" % inv["invite"]["id"]).status, 404)
        self.assertEqual(self.b.get("/api/invites").json["invites"], [])
        # still valid for tenant A
        self.assertEqual(Client(self.ts).post("/api/invites/preview", {"token": inv["token"]}).status, 200)

    def test_token_cannot_be_replayed_against_another_tenant(self):
        inv = self.a.post("/api/invites", {"email": "h2@example.com", "role": "manager"}).json
        secret = inv["token"].split(".")[1]
        swapped = "%s.%s" % (self.b.tenant_id, secret)
        c = Client(self.ts)
        r = c.post("/api/invites/accept", {"token": swapped, "name": "X", "password": PASSWORD})
        self.assertEqual(r.status, 400)

    def test_cannot_modify_other_tenants_user(self):
        a_user = self.a.get("/api/me").json["user"]["id"]
        self.assertEqual(self.b.put("/api/users/%s" % a_user, {"active": False}).status, 404)
        self.assertEqual(self.a.get("/api/me").status, 200)

    def test_invited_user_belongs_only_to_inviting_tenant(self):
        inv = self.a.post("/api/invites", {"email": "h3@example.com", "role": "technician"}).json
        c = Client(self.ts)
        r = c.post("/api/invites/accept", {"token": inv["token"], "name": "H3", "password": PASSWORD})
        self.assertEqual(r.status, 200)
        c.csrf = r.json["csrf"]
        self.assertEqual(c.get("/api/me").json["business"]["name"], "Alpha Team")
        b_emails = [u["email"] for u in self.b.get("/api/users").json["users"]]
        self.assertNotIn("h3@example.com", b_emails)


if __name__ == "__main__":
    unittest.main()
