$ErrorActionPreference = "Stop"
$repo = "Ev3rGan/ai-ledger"
$parent = 1
$label = "ready-for-agent"

function Invoke-GhCapture {
    param([string[]]$Arguments)

    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & gh @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    return [pscustomobject]@{
        output = @($output)
        exitCode = $exitCode
    }
}

$databaseIdCache = @{}
function Get-IssueDatabaseId {
    param([int]$IssueNumber)

    if ($databaseIdCache.ContainsKey($IssueNumber)) {
        return [int64]$databaseIdCache[$IssueNumber]
    }

    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $result = Invoke-GhCapture -Arguments @(
            "api",
            "repos/$repo/issues/$IssueNumber",
            "--jq", ".id"
        )
        $value = ($result.output -join "").Trim()
        if ($result.exitCode -eq 0 -and $value -match "^\d+$") {
            $databaseIdCache[$IssueNumber] = [int64]$value
            return [int64]$value
        }
        if ($attempt -lt 3) { Start-Sleep -Seconds 2 }
    }

    throw "Could not resolve database ID for blocker #$IssueNumber after three attempts."
}

$tickets = @(
    [pscustomobject]@{
        Id = "T01"
        Title = "T01 — Persist one deterministic sample Story"
        Blockers = @()
        Behavior = '`ai-intel-agent run --sample` persists the fixed sample data as a Candidate, Document Version, Story, atomic Claim, anchored Evidence Span, and structured trace.'
        Acceptance = "Given the shared fixed clock, real test PostgreSQL/pgvector, deterministic sample data, and Fake external adapters, run the sample CLI twice; after the second run exactly one corresponding set of domain records remains and the output traces to the same Evidence Span."
        NonGoals = @("Web pages or administrator review", "Real sources or model providers", "A compatibility layer for placeholder classes", "Empty implementations of every future MVP table")
    },
    [pscustomobject]@{
        Id = "T02"
        Title = "T02 — Audit first-wave Source Definitions"
        Blockers = @()
        Behavior = "Produce an executable activation audit for every first-wave Source Definition."
        Acceptance = "Run the audit workflow and produce an approved, metadata-only, blocked, or needs-verification conclusion for every candidate source, including its entry point, language, Topic scope, robots and terms findings, storage and excerpt policy, and pause conditions."
        NonGoals = @("Implementing acquisition adapters", "Assuming unknown permissions have been granted", "Testing Story generation or summary quality", "Using production credentials")
    },
    [pscustomobject]@{
        Id = "T03"
        Title = "T03 — Benchmark Document extraction"
        Blockers = @()
        Behavior = "Use the fixed 60-URL corpus to compare local and managed extraction paths and recommend at most one managed fallback."
        Acceptance = "Run one standalone benchmark command and generate a versioned report comparing HTTP plus Trafilatura, Playwright plus Trafilatura, Firecrawl, and Tavily for body extraction, metadata, noise, provenance anchoring, repeatability, latency, and cost across every corpus category."
        NonGoals = @("Activating production Source Definitions", "Integrating with Collection Runs", "Freezing the provisional Q144 thresholds as product requirements", "Allowing rewritten text to become Evidence")
    },
    [pscustomobject]@{
        Id = "T04"
        Title = "T04 — Evaluate DeepSeek and Kimi task routes"
        Blockers = @()
        Behavior = "Compare DeepSeek and Kimi on the fixed human-approved corpus and produce versioned routing recommendations for the agreed task classes."
        Acceptance = "Run one standalone evaluation command and report critical gates, quality, latency, and cost for classification, Chinese summarization, Claim verification, simple questions, and complex reasoning, selecting only candidates that pass every critical gate."
        NonGoals = @("Connecting models to the production application", "Depending on the Research UI or Intelligence workflow", "Making current model IDs permanent architecture", "Letting aggregate scores hide factual, citation, abstention, or structure failures")
    },
    [pscustomobject]@{
        Id = "T05"
        Title = "T05 — Calibrate multilingual Retrieval Profiles"
        Blockers = @()
        Behavior = "Use the fixed retrieval corpus to evaluate Embedding, Reranker, Chunk, and fusion candidates and export a versioned Retrieval Profile."
        Acceptance = "Run one standalone calibration command and report cross-language retrieval, exact technical-entity retrieval, Evidence Span recall, CPU resources, and latency, then export one loadable Retrieval Profile."
        NonGoals = @("Changing public Browse or Research behavior", "Requiring the application database to be implemented", "Freezing temporary Chunk sizes, weights, or top-N values", "Introducing a separate vector database")
    },
    [pscustomobject]@{
        Id = "T06"
        Title = "T06 — Benchmark the Hong Kong runtime"
        Blockers = @()
        Behavior = "Compare Hong Kong runtime candidates with fixed probes and a representative container workload, producing deployment evidence and a recommendation."
        Acceptance = "Run the same network, SSE, source egress, model API, OAuth, resource, and cost probes from each candidate Hong Kong node and produce a reproducible comparison report."
        NonGoals = @("Deploying the complete MVP", "Depending on real acquisition, Research, or OAuth features", "Running production database migrations", "Treating current cloud prices as permanent facts")
    },
    [pscustomobject]@{
        Id = "T07"
        Title = "T07 — Review Stories and publish one Digest"
        Blockers = @("T01")
        Behavior = "An administrator reviews deterministic sample Stories through the top-level application interface and publishes one Digest."
        Acceptance = "Given sample Stories in accepted, rejected, and unreviewed states, a Fake administrator completes review and composition; the system publishes one stably identified Digest containing only accepted Stories and records the complete audit trail."
        NonGoals = @("Public pages or RSS", "Real GitHub OAuth", "Real source acquisition", "Post-publication revisions")
    },
    [pscustomobject]@{
        Id = "T08"
        Title = "T08 — Read a published Digest through Web and RSS"
        Blockers = @("T07")
        Behavior = "An anonymous visitor reads the same published Digest through the home page, Digest page, Story page, basic Browse, and RSS."
        Acceptance = "Using the application test client, open one published sample Digest from the home page, follow it to its Digest and Story pages, and read its RSS item; every surface shows consistent public content, Evidence state, and source links without exposing private source text."
        NonGoals = @("Full-text or vector retrieval", "Research Agent behavior", "Administrator OAuth", "Story revisions")
    },
    [pscustomobject]@{
        Id = "T09"
        Title = "T09 — Collect RSS and Atom Candidates"
        Blockers = @("T01", "T02")
        Behavior = "Approved RSS and Atom Source Definitions create persistent Candidates and Document Versions."
        Acceptance = "Run one Collection with a successful feed fixture and a failing feed fixture; successful content is persisted, the Run is partial, retry creates a linked new Run, and repeated content remains idempotent."
        NonGoals = @("Requiring an administrator review page", "Generating or publishing a Digest", "HTML, arXiv, or GitHub acquisition", "Calling a real model")
    },
    [pscustomobject]@{
        Id = "T10"
        Title = "T10 — Collect arXiv Research Candidates"
        Blockers = @("T09")
        Behavior = "arXiv query results enter the existing acquisition queue as Research Candidates."
        Acceptance = "Run a Collection with a fixed arXiv API response; the system persists paper metadata, abstract, original link, timestamps, and provenance, and a repeat run creates no duplicate Candidate."
        NonGoals = @("Downloading or publicly hosting paper full text", "Model-generated paper summaries", "Research ranking changes", "Additional academic sources")
    },
    [pscustomobject]@{
        Id = "T11"
        Title = "T11 — Collect curated GitHub Releases"
        Blockers = @("T09")
        Behavior = "Eligible Releases from allowlisted repositories enter the existing acquisition queue as Candidates."
        Acceptance = "Run a Collection with a fixed GitHub API response containing a stable release, draft, prerelease, and automated build; only releases satisfying repository rules are persisted and their canonical repository identity is preserved."
        NonGoals = @("Discovering new repositories", "Using stars as factual Evidence", "Tool recommendations", "Scraping GitHub HTML")
    },
    [pscustomobject]@{
        Id = "T12"
        Title = "T12 — Collect approved official HTML sources"
        Blockers = @("T03", "T09")
        Behavior = "An approved official source without a stable feed enters the acquisition queue through the selected extraction path."
        Acceptance = "Collect one fixed official HTML page through the benchmark-selected adapter order; the system creates a Document Version and proves that its Evidence Span resolves exactly to normalized source text."
        NonGoals = @("Chinese professional media", "Rerunning the extraction benchmark", "Using an LLM to complete missing body text", "Bulk activation of all official sources")
    },
    [pscustomobject]@{
        Id = "T13"
        Title = "T13 — Collect approved Chinese media sources"
        Blockers = @("T03", "T09")
        Behavior = "One approved dynamic or semi-dynamic Chinese media source enters the acquisition queue."
        Acceptance = "Collect one fixed Chinese media fixture; the system preserves Chinese body text, publication time, canonical URL, and source boundary while refusing login, paid, or prohibited paths."
        NonGoals = @("Implementing every Chinese media adapter", "Generating Chinese summaries", "Re-deciding source permission", "Using a model to repair missing body text")
    },
    [pscustomobject]@{
        Id = "T14"
        Title = "T14 — Correct published content without rewriting history"
        Blockers = @("T01")
        Behavior = "A published Story can receive an immutable Story Revision and public Correction Notice."
        Acceptance = "Given a published Story fixture, submit one factual correction through the top-level application interface; the application test client sees the new Revision and Correction Notice while the original Revision remains in the audit history."
        NonGoals = @("Depending on Digest publication", "Complete public-site navigation", "Treating a new real-world event as a revision", "A bulk editor for non-factual presentation changes")
    },
    [pscustomobject]@{
        Id = "T15"
        Title = "T15 — Browse accepted Stories with filters and full-text search"
        Blockers = @("T08")
        Behavior = "An anonymous visitor filters and full-text searches publicly eligible Stories by Topic, Entity, Publisher, time, and review state."
        Acceptance = "Submit one Browse request containing a full-text term and metadata filters through the application test client; only matching accepted Stories are returned, with rejected and default-hidden unreviewed Stories excluded."
        NonGoals = @("pgvector retrieval", "Embedding or Reranker calls", "Research Answers", "Ranking-weight calibration")
    },
    [pscustomobject]@{
        Id = "T16"
        Title = "T16 — Upgrade Browse to hybrid retrieval"
        Blockers = @("T05", "T15")
        Behavior = "The existing Browse request fuses full-text, vector, and exact-Entity results through a versioned Retrieval Profile."
        Acceptance = "Submit a fixed query whose expected Story cannot be recalled by full-text search alone; the application test client receives that Story and its trace records the FTS, pgvector, Entity, fusion, and reranking stages under the selected Retrieval Profile."
        NonGoals = @("Generating Research Answers", "Rerunning the retrieval benchmark", "Displaying a Chunk as Evidence", "Adding an external vector database")
    },
    [pscustomobject]@{
        Id = "T17"
        Title = "T17 — Answer one simple Research question"
        Blockers = @("T16")
        Behavior = "The Research Agent answers one simple factual question from accepted knowledge with reproducible citations."
        Acceptance = "Submit one fixed question through the application test client; the Fake Model returns the expected Research Answer with citations to the exact accepted Story, Claim, Evidence Span, and Revision, and no live-web tool is called."
        NonGoals = @("Comparisons or timelines", "Conflict and counter-evidence handling", "Real model providers", "Tool recommendations")
    },
    [pscustomobject]@{
        Id = "T18"
        Title = "T18 — Compare accepted Stories and build a timeline"
        Blockers = @("T17")
        Behavior = "The Research Agent compares multiple accepted Stories and constructs a timeline using event time."
        Acceptance = "Submit one fixed comparison question through the application test client; the Fake Model returns a comparison and timeline covering the expected Stories while explicitly distinguishing event, publication, and discovery times."
        NonGoals = @("Conflict adjudication", "Counter-evidence discovery", "Insufficient-evidence abstention", "Real model providers")
    },
    [pscustomobject]@{
        Id = "T19"
        Title = "T19 — Surface conflicts, counter-evidence, and abstention"
        Blockers = @("T17")
        Behavior = "One Research request surfaces conflicting Claims, includes counter-evidence, and abstains where Evidence is insufficient."
        Acceptance = "Given a fixed corpus containing supporting, contradicting, and missing Evidence, submit one Research request through the application test client and receive an explicit conflict explanation, citations for both sides, and an abstention instead of a forced conclusion."
        NonGoals = @("General comparison or timeline generation", "Tool recommendations", "Live-web access", "Model-routing changes")
    },
    [pscustomobject]@{
        Id = "T20"
        Title = "T20 — Publish one Community Signal without promoting it to fact"
        Blockers = @("T01")
        Behavior = "Public community material forms a Community Signal but cannot independently establish a product or performance Claim."
        Acceptance = "Given one high-engagement community discussion fixture, run the top-level Intelligence operation and produce a displayable Community Signal; attempting to qualify the same material as sole support for a performance Claim is rejected."
        NonGoals = @("Real Hacker News or ModelScope acquisition", "Tool recommendations", "Complete Popularity ranking", "Changing Publisher Authority from engagement")
    },
    [pscustomobject]@{
        Id = "T21"
        Title = "T21 — Recommend tools for one declared technology stack"
        Blockers = @("T17")
        Behavior = "The Research Agent recommends tools for a declared technology stack and falls back to a dated popular-tools overview when no stack is supplied."
        Acceptance = "Submit fixed Research requests with and without a technology stack through the application test client; the first returns constraint-matched recommendations, while the second returns a dated overview with Popularity provenance and makes no personalization claim."
        NonGoals = @("Depending on GitHub Release acquisition", "Long-term user profiles", "Treating stars or Community Signals as quality proof", "Live-web access")
    },
    [pscustomobject]@{
        Id = "T22"
        Title = "T22 — Execute Research through the real Provider Gateway"
        Blockers = @("T04", "T17")
        Behavior = "The existing simple Research request replaces the Fake Model with a versioned real DeepSeek or Kimi route."
        Acceptance = "In a credential-enabled controlled test, submit one fixed question through the application test client; the Provider Gateway selects the evaluated route, validates structured output, and returns a cited Research Answer with provider and configuration trace data."
        NonGoals = @("Monthly cost metering and degradation", "Re-selecting model routes", "Requiring comparison, timeline, or conflict features", "OpenAI integration")
    },
    [pscustomobject]@{
        Id = "T23"
        Title = "T23 — Meter external API spending and degrade safely"
        Blockers = @("T08", "T22")
        Behavior = "All usage-metered external APIs share one budget ledger and degrade AI behavior at the agreed thresholds without hiding historical publications."
        Acceptance = "Feed deterministic billing events across 70, 85, 95, and 100 percent of the monthly cap; the application test client observes the expected feature degradation while every published Digest and Story remains readable."
        NonGoals = @("Provider SDK implementation", "Selecting final production traffic limits", "Including infrastructure cost", "Changing historical publications")
    },
    [pscustomobject]@{
        Id = "T24"
        Title = "T24 — Authenticate the administrator with GitHub OAuth"
        Blockers = @("T07")
        Behavior = "GitHub OAuth grants existing administrator behavior only to the allowlisted numeric GitHub user ID."
        Acceptance = "Use the application test client and Fake OAuth to simulate an allowlisted user, a non-allowlisted user, and an anonymous visitor; only the allowlisted user can review Stories and publish a Digest."
        NonGoals = @("Anonymous Research rate limiting", "Public registration or password login", "Persistent user profiles", "Production callback-network validation")
    },
    [pscustomobject]@{
        Id = "T25"
        Title = "T25 — Rate-limit anonymous Research requests"
        Blockers = @("T17")
        Behavior = "Anonymous Research requests are limited by an irreversible access identifier."
        Acceptance = "Submit more than the configured allowance from one fixed rate-limit identifier through the application test client; allowed requests succeed, excess requests receive the explicit degraded response, and no direct IP address is stored."
        NonGoals = @("OAuth", "Choosing the final production allowance", "Retention cleanup", "Changing the model budget policy")
    },
    [pscustomobject]@{
        Id = "T26"
        Title = "T26 — Expire anonymous bodies and redact traces"
        Blockers = @("T17")
        Behavior = "Anonymous question and answer bodies expire after seven days, aggregate metrics remain for ninety days, and sensitive trace fields are redacted."
        Acceptance = "Advance the shared fixed clock beyond both retention boundaries and run the maintenance operation; expired bodies are no longer readable through the application test client, permitted aggregates remain, and traces contain no prompt, secret, or sensitive tool parameter."
        NonGoals = @("Anonymous rate limiting", "OAuth", "Selecting an external log platform", "Registered-user data")
    },
    [pscustomobject]@{
        Id = "T27"
        Title = "T27 — Deploy the sample-mode service with off-machine backups"
        Blockers = @("T01", "T06")
        Behavior = "Deploy the deterministic sample-mode service to the selected Hong Kong node and create one daily off-machine PostgreSQL logical backup."
        Acceptance = "Deploy from a clean host; the public health check and sample CLI use the same PostgreSQL/pgvector database, and triggering backup creates a new off-machine object with integrity metadata."
        NonGoals = @("Requiring every MVP feature", "Recovery or rollback drills", "Claiming all launch gates have passed", "High availability or multi-region deployment")
    },
    [pscustomobject]@{
        Id = "T28"
        Title = "T28 — Prove database recovery and application rollback"
        Blockers = @("T27")
        Behavior = "An operator restores an empty drill environment from an off-machine backup and rolls a failed release back to the previous application image."
        Acceptance = "In one drill, clear only the disposable drill environment, restore the selected backup, rebuild pgvector indexes, simulate a failed deployment, switch to the previous image, and rerun the deterministic sample-mode system acceptance scenario successfully."
        NonGoals = @("Destructive recovery against live production data", "Automatic failover", "A formal uptime SLA", "Selecting a different cloud provider")
    }
)

$issueMap = @{}
$publication = @()

$knownIssues = $null
for ($attempt = 1; $attempt -le 3 -and $null -eq $knownIssues; $attempt++) {
    $listResult = Invoke-GhCapture -Arguments @(
        "issue", "list",
        "--repo", $repo,
        "--state", "all",
        "--limit", "500",
        "--json", "number,title,url,state,labels,body"
    )
    if ($listResult.exitCode -eq 0) {
        try {
            $knownIssues = ($listResult.output -join "`n") | ConvertFrom-Json
        } catch {
            $knownIssues = $null
        }
    }
    if ($null -eq $knownIssues -and $attempt -lt 3) { Start-Sleep -Seconds 2 }
}
if ($null -eq $knownIssues) { throw "Could not query existing issues after three attempts." }

foreach ($ticket in $tickets) {
    $matches = @($knownIssues | Where-Object { $_.title -ceq $ticket.Title })
    if ($matches.Count -gt 1) { throw "More than one issue has the exact title: $($ticket.Title)" }

    $blockerNumbers = @($ticket.Blockers | ForEach-Object {
        if (-not $issueMap.ContainsKey($_)) { throw "Missing blocker mapping $_ for $($ticket.Id)." }
        [int]$issueMap[$_].number
    })

    $bodyLines = @(
        "## Parent",
        "",
        "Parent: #$parent",
        "",
        "## What to build",
        "",
        $ticket.Behavior,
        "",
        "## Acceptance criteria",
        "",
        "- [ ] $($ticket.Acceptance)",
        "",
        "## Non-goals",
        ""
    )
    $bodyLines += @($ticket.NonGoals | ForEach-Object { "- $_" })
    $bodyLines += @("", "## Blocked by", "")
    if ($blockerNumbers.Count -eq 0) {
        $bodyLines += "- None — can start immediately"
    } else {
        $bodyLines += @($blockerNumbers | ForEach-Object { "- #$_" })
    }
    $body = $bodyLines -join "`n"

    if ($matches.Count -eq 1) {
        $issue = $matches[0]
        $existingLabels = @($issue.labels | ForEach-Object { $_.name })
        if ($label -notin $existingLabels) {
            gh issue edit $issue.number --repo $repo --add-label $label | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not add label to existing issue #$($issue.number)." }
        }
        $action = "reused"
    } else {
        $url = gh issue create --repo $repo --title $ticket.Title --body $body --label $label
        if ($LASTEXITCODE -ne 0) { throw "Could not create $($ticket.Id)." }
        $number = [int](($url.Trim().TrimEnd('/') -split '/')[-1])
        $issue = [pscustomobject]@{ number = $number; url = $url.Trim(); title = $ticket.Title }
        $knownIssues += $issue
        $action = "created"
    }

    $issueMap[$ticket.Id] = [pscustomobject]@{
        number = [int]$issue.number
        url = [string]$issue.url
        title = $ticket.Title
    }
    $publication += [pscustomobject]@{
        id = $ticket.Id
        number = [int]$issue.number
        url = [string]$issue.url
        title = $ticket.Title
        blockerNumbers = $blockerNumbers
        action = $action
    }
    Write-Output ("PUBLISHED {0} #{1} {2}" -f $ticket.Id, $issue.number, $action)
}

$edgeResults = @()
foreach ($ticket in $tickets) {
    $childNumber = [int]$issueMap[$ticket.Id].number
    foreach ($blockerId in $ticket.Blockers) {
        $blockerNumber = [int]$issueMap[$blockerId].number
        $blockerDbId = Get-IssueDatabaseId -IssueNumber $blockerNumber

        $native = $false
        $detail = ""
        $existingResult = Invoke-GhCapture -Arguments @(
            "api",
            "repos/$repo/issues/$childNumber/dependencies/blocked_by",
            "--paginate",
            "--jq",
            ".[ ].number" -replace " ", ""
        )
        if ($existingResult.exitCode -eq 0) {
            $existingNumbers = @($existingResult.output | ForEach-Object { if ($_ -match "^\d+$") { [int]$_ } })
            if ($blockerNumber -in $existingNumbers) {
                $native = $true
                $detail = "native-existing"
            }
        }

        if (-not $native) {
            for ($attempt = 1; $attempt -le 3 -and -not $native; $attempt++) {
                $postResult = Invoke-GhCapture -Arguments @(
                    "api",
                    "--method", "POST",
                    "repos/$repo/issues/$childNumber/dependencies/blocked_by",
                    "-F", "issue_id=$blockerDbId"
                )
                $postText = $postResult.output -join "`n"
                if ($postResult.exitCode -eq 0 -or $postText -match "already exists|already has this dependency") {
                    $native = $true
                    $detail = if ($postResult.exitCode -eq 0) { "native-created" } else { "native-existing" }
                } elseif ($attempt -lt 3) {
                    Start-Sleep -Seconds 2
                } else {
                    $detail = $postText
                }
            }
        }

        $edgeResults += [pscustomobject]@{
            childId = $ticket.Id
            childNumber = $childNumber
            blockerId = $blockerId
            blockerNumber = $blockerNumber
            native = $native
            detail = $detail
        }
        Write-Output ("EDGE {0} #{1} blocked-by {2} #{3} native={4}" -f $ticket.Id, $childNumber, $blockerId, $blockerNumber, $native)
    }
}

$review = @()
foreach ($published in $publication) {
    $view = @($knownIssues | Where-Object { $_.title -ceq $published.title })[0]
    if ($null -eq $view) { throw "Could not review issue #$($published.number) from the issue-list snapshot." }
    $labelNames = @($view.labels | ForEach-Object { $_.name })
    $bodyBlockers = @()
    $blockedSection = [regex]::Match($view.body, "(?ms)^## Blocked by\s*(.*)$").Groups[1].Value
    if ($blockedSection) {
        $bodyBlockers = @([regex]::Matches($blockedSection, "#(\d+)") | ForEach-Object { [int]$_.Groups[1].Value })
    }
    $review += [pscustomobject]@{
        id = $published.id
        number = [int]$view.number
        url = [string]$view.url
        title = [string]$view.title
        state = [string]$view.state
        labels = $labelNames
        blockerNumbers = $published.blockerNumbers
        bodyBlockerNumbers = $bodyBlockers
        parentPresent = $view.body -match "(?m)^Parent: #1$"
        labelPresent = $label -in $labelNames
        titleMatches = $view.title -ceq $published.title
        blockersMatch = (@($published.blockerNumbers) -join ",") -eq (@($bodyBlockers) -join ",")
    }
}

$receipt = [pscustomobject]@{
    parent = $parent
    totalTickets = $review.Count
    totalEdges = $edgeResults.Count
    nativeEdges = @($edgeResults | Where-Object native).Count
    textOnlyEdges = @($edgeResults | Where-Object { -not $_.native }).Count
    tickets = $review
    edges = $edgeResults
}
foreach ($item in $review) {
    Write-Output ("REVIEW {0} #{1} labels={2} blockers={3} parent={4} title={5} blockersMatch={6}" -f $item.id, $item.number, ($item.labels -join ","), ($item.blockerNumbers -join ","), $item.parentPresent, $item.titleMatches, $item.blockersMatch)
}
Write-Output ("SUMMARY tickets={0} edges={1} native={2} textOnly={3}" -f $receipt.totalTickets, $receipt.totalEdges, $receipt.nativeEdges, $receipt.textOnlyEdges)
