# AI Intelligence Domain

This context describes how public AI information becomes traceable intelligence and how that intelligence supports evidence-grounded research. It separates publication, collection, retrieval, evidence, and editorial concepts that are easy to conflate.

## Acquisition

**Publisher**:
The organization responsible for publishing a piece of source content.
_Avoid_: Source, outlet, website

**Source Definition**:
A configured entry point and policy for discovering content from a Publisher. A Publisher may have several Source Definitions.
_Avoid_: Source, publisher, URL

**Candidate**:
A newly discovered content lead whose relevance and validity have not yet been accepted.
_Avoid_: Story, article, news item

**Document**:
A normalized source artifact obtained from one canonical location, such as an article, paper, release, or repository page.
_Avoid_: Story, article, page

**Document Version**:
An immutable snapshot of a Document at a particular observed revision.
_Avoid_: Document, update

**Collection Run**:
One immutable record of a scheduled or manually initiated attempt to discover and acquire Candidates from a defined set of Source Definitions. A retry is a related new Collection Run.
_Avoid_: Crawl, batch, job

## Intelligence

**Story**:
A bounded real-world AI information event synthesized from one or more Documents. A materially new action or outcome is a new Story, even when it relates to an earlier one.
_Avoid_: Article, document, topic, news item

**Story Revision**:
An immutable approved or published rendition of a Story's editorial content. A new real-world action is a related Story, not a Story Revision.
_Avoid_: Document Version, Story, edit

**Claim**:
A fact-like statement about a Story whose support, contradiction, and verification status can be assessed independently.
_Avoid_: Summary, opinion, sentence

**Evidence Span**:
An immutable excerpt from a specific Document Version that supports or contradicts a Claim.
_Avoid_: Chunk, citation, search result

**Evidence Role**:
The relationship of an Evidence Span's source to a specific Claim: Primary, Independent, Secondary, or Community. A source can have different Evidence Roles for different Claims.
_Avoid_: Authority, Support Strength, Publisher type

**Chunk**:
A rebuildable segment of a Document Version used to retrieve potentially relevant material. A Chunk is not evidence until selected as an Evidence Span.
_Avoid_: Evidence, quote, claim

**Entity**:
A typed, normalized named subject of Stories, such as a company, person, model family, model version, product, project, repository, paper, or organization.
_Avoid_: Tag, keyword

**Community Signal**:
A traceable indication of what a public technical community is discussing, experiencing, or contesting. It does not by itself establish the underlying product, performance, business, or safety fact.
_Avoid_: Fact, Claim, popularity score, rumor

## Classification and Editorial Work

**Topic**:
A subject classification for a Story. Every Story has one primary Topic and may have secondary Topics.
_Avoid_: Editorial label, section, tag

**Editorial Label**:
An editorial treatment such as Focus or Trend that controls presentation without changing a Story's Topic.
_Avoid_: Topic, category

**Digest**:
A reviewed daily composition of accepted Stories arranged for public reading.
_Avoid_: Story, feed, collection run

**Digest Revision**:
An immutable published rendition of a Digest for its original publication date.
_Avoid_: Digest, new edition, repost

**Editorial Window**:
The fixed Asia/Shanghai time interval that determines which eligible Stories are considered for one dated Digest. It does not replace source, event, discovery, or processing timestamps.
_Avoid_: Publication date, Collection Run, calendar day

**Correction Notice**:
A public explanation of a substantive factual or evidentiary change between published Story Revisions or Digest Revisions.
_Avoid_: Edit history, changelog, silent fix

**System Analysis**:
An explicitly marked interpretation of why a Story matters, kept distinct from source-backed Claims.
_Avoid_: Fact, publisher opinion

## Research

**Query Intent**:
A structured interpretation of a research question that identifies its task type, scope, entities, time range, and execution budget.
_Avoid_: Prompt, query string

**Evidence Set**:
The structured collection of supporting, contradicting, and missing evidence assembled for a research question.
_Avoid_: Context window, search results

**Research Answer**:
An AI-generated response grounded in accepted knowledge, with reproducible provenance to the Stories, Claims, Evidence Spans, and versions used.
_Avoid_: Chat message, model output

## Quality Signals

**Authority**:
A Publisher-level assessment of source trustworthiness.
_Avoid_: Confidence, impact

**Relevance**:
A Candidate-level assessment of fit with the product's AI intelligence scope.
_Avoid_: Importance, authority

**Impact**:
A Story-level assessment of likely significance to AI developers and technical practitioners.
_Avoid_: Popularity, confidence

**Novelty**:
A Story-level assessment of how much new information it adds relative to known Stories.
_Avoid_: Recency, impact

**Popularity**:
A time-bounded, reproducible assessment of observable attention to a Story, tool, or Entity. Popularity may influence presentation but never establishes factual support or eligibility by itself.
_Avoid_: Impact, Authority, Confidence, quality

**Confidence**:
A Claim-level assessment of how strongly available evidence justifies the Claim.
_Avoid_: Authority, support strength

**Support Strength**:
An Evidence Span-level assessment of how directly it supports or contradicts a Claim.
_Avoid_: Confidence, relevance
