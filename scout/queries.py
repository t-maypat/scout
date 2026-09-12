"""GraphQL documents. Kept apart from the scoring so a query can be fixed without
touching the definition of a metric, and so tests can feed recorded responses straight
into scout.metrics with no client at all.
"""

from __future__ import annotations

OVERVIEW = """
query Overview($owner: String!, $name: String!, $prs: Int!) {
  rateLimit { cost remaining }
  repository(owner: $owner, name: $name) {
    nameWithOwner
    description
    stargazerCount
    forkCount
    isArchived
    isFork
    pushedAt
    hasIssuesEnabled
    primaryLanguage { name }
    licenseInfo { spdxId }
    defaultBranchRef { name }
    openIssues: issues(states: OPEN) { totalCount }
    openPRs: pullRequests(states: OPEN) { totalCount }
    merged: pullRequests(
      states: MERGED
      first: $prs
      orderBy: { field: CREATED_AT, direction: DESC }
    ) {
      nodes {
        number
        createdAt
        mergedAt
        authorAssociation
        additions
        deletions
        author { login }
      }
    }
  }
}
"""

ISSUES = """
query Issues($owner: String!, $name: String!, $n: Int!) {
  rateLimit { cost remaining }
  repository(owner: $owner, name: $name) {
    issues(first: $n, orderBy: { field: CREATED_AT, direction: DESC }) {
      nodes {
        number
        title
        url
        createdAt
        closedAt
        authorAssociation
        author { login }
        labels(first: 12) { nodes { name } }
        comments(first: 15) {
          nodes {
            createdAt
            authorAssociation
            author { login }
          }
        }
      }
    }
  }
}
"""

# Least-recently-updated first: the stalest rows are the ones worth looking at, and they
# are the ones a DESC sort would never reach.
STALE = """
query Stale($owner: String!, $name: String!, $n: Int!) {
  rateLimit { cost remaining }
  repository(owner: $owner, name: $name) {
    assigned: issues(
      states: OPEN
      first: $n
      orderBy: { field: UPDATED_AT, direction: ASC }
    ) {
      nodes {
        number
        title
        url
        createdAt
        updatedAt
        assignees(first: 5) { nodes { login } }
        crossRefs: timelineItems(itemTypes: [CROSS_REFERENCED_EVENT]) { totalCount }
      }
    }
    openPRs: pullRequests(
      states: OPEN
      first: $n
      orderBy: { field: UPDATED_AT, direction: ASC }
    ) {
      nodes {
        number
        title
        url
        createdAt
        updatedAt
        isDraft
        authorAssociation
        author { login }
      }
    }
  }
}
"""
