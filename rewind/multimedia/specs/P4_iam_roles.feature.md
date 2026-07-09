# P4 · IAM, roles & tenant isolation

## 1. Intent (why)
Teams need roles and hard tenant boundaries. This is the security backbone for
the workspace layer (Day 4 Pillars 2 & 5): least privilege, isolation, audit.

## 2. Scope
**In:** roles (owner, editor, contributor, viewer); membership checks on every
workspace/event action; tenant partitioning at the query layer; immutable audit log.
**Out:** SSO provider integrations (later); billing entitlements (P5).

## 3. Contract (schema)
```yaml
roles:
  owner:       [manage_members, billing, create_event, run_pipeline, delete, view]
  editor:      [create_event, run_pipeline, curate, view]
  contributor: [upload_via_share_code]      # no dashboard access
  viewer:      [view_journal]
data:
  Membership: { workspace_id, user_id, role }
  AuditEntry: { id, workspace_id, actor_id, action, target, at, immutable: true }
enforcement:
  where: "query/service layer, not UI"      # UI hiding is not security
  principle: least_privilege_JIT            # downscope tokens per action
```

## 4. Behavior (BDD)
```gherkin
Feature: Roles and isolation

  Scenario: Least privilege
    Given a user with role "viewer"
    When they call /generate (run pipeline)
    Then the request is denied (403)

  Scenario: Tenant isolation
    Given user is a member of Workspace A only
    When they request Workspace B's events
    Then the request is denied and nothing from B is returned

  Scenario: Audit trail
    When an owner deletes an event
    Then an immutable AuditEntry records actor, action, target, and time
```

## 5. Eval cases (gate)
```yaml
eval:
  method: [pytest]
  golden_dataset: eval/datasets/p4-iam.json
  cases:
    - id: rbac_matrix
      expected_output: { all_role_action_pairs_enforced: true }
    - id: cross_tenant_denied
      expected_output: { leak: false }
    - id: audit_complete
      expected_output: { every_destructive_action_logged: true }
```

## 6. Security (7-pillar)
- Pillar 5 IAM: unique identities, least-privilege, JIT downscoping; no ambient authority.
- Pillar 2 Data: tenant partitioning; Pillar 7 Governance: immutable audit trail.
- Confused-deputy guard: agents act with dedicated identities, not a user's broad rights.

## 7. Done when
- [ ] RBAC matrix enforced server-side · [ ] cross-tenant leak tests pass · [ ] audit complete
- [ ] Authority tier: **action_allowed** (security-critical — full eval coverage required)
