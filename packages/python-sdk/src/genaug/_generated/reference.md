# Reference
## Agent
<details><summary><code>client.agent.<a href="src/general_augment/agent/client.py">memory_profile</a>(...) -> MemoryProfileResponse</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Return one tenant user's stored profile and recent memory facts.
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```python
from general_augment import GeneralAugmentClient
from general_augment.environment import GeneralAugmentClientEnvironment

client = GeneralAugmentClient(
    admin_key="<value>",
    environment=GeneralAugmentClientEnvironment.PRODUCTION,
)

client.agent.memory_profile(
    user_id="user_id",
)

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**user_id:** `str` 
    
</dd>
</dl>

<dl>
<dd>

**agent_run_id:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**request_options:** `typing.Optional[RequestOptions]` — Request-specific configuration.
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.agent.<a href="src/general_augment/agent/client.py">search_memory</a>(...) -> MemorySearchResponse</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Semantically search one tenant user's memories.
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```python
from general_augment import GeneralAugmentClient
from general_augment.environment import GeneralAugmentClientEnvironment

client = GeneralAugmentClient(
    admin_key="<value>",
    environment=GeneralAugmentClientEnvironment.PRODUCTION,
)

client.agent.search_memory(
    user_id="user_id",
)

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**user_id:** `str` 
    
</dd>
</dl>

<dl>
<dd>

**agent_run_id:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**collection_key:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**created_after:** `typing.Optional[datetime.datetime]` 
    
</dd>
</dl>

<dl>
<dd>

**fact_type:** `typing.Optional[MemorySearchRequestFactType]` 
    
</dd>
</dl>

<dl>
<dd>

**include_sensitive:** `typing.Optional[bool]` 
    
</dd>
</dl>

<dl>
<dd>

**limit:** `typing.Optional[int]` 
    
</dd>
</dl>

<dl>
<dd>

**min_importance:** `typing.Optional[float]` 
    
</dd>
</dl>

<dl>
<dd>

**min_similarity:** `typing.Optional[float]` 
    
</dd>
</dl>

<dl>
<dd>

**query:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**source:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**request_options:** `typing.Optional[RequestOptions]` — Request-specific configuration.
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.agent.<a href="src/general_augment/agent/client.py">store_memory</a>(...) -> MemoryStoreResponse</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Store one explicit memory fact for a tenant user.
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```python
from general_augment import GeneralAugmentClient
from general_augment.environment import GeneralAugmentClientEnvironment

client = GeneralAugmentClient(
    admin_key="<value>",
    environment=GeneralAugmentClientEnvironment.PRODUCTION,
)

client.agent.store_memory(
    fact="fact",
    user_id="user_id",
)

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**fact:** `str` 
    
</dd>
</dl>

<dl>
<dd>

**user_id:** `str` 
    
</dd>
</dl>

<dl>
<dd>

**agent_run_id:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**collection_key:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**fact_type:** `typing.Optional[MemoryStoreRequestFactType]` 
    
</dd>
</dl>

<dl>
<dd>

**idempotency_key:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**importance_score:** `typing.Optional[float]` 
    
</dd>
</dl>

<dl>
<dd>

**metadata:** `typing.Optional[typing.Dict[str, typing.Any]]` 
    
</dd>
</dl>

<dl>
<dd>

**source:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**user_profile:** `typing.Optional[typing.Dict[str, typing.Any]]` 
    
</dd>
</dl>

<dl>
<dd>

**request_options:** `typing.Optional[RequestOptions]` — Request-specific configuration.
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.agent.<a href="src/general_augment/agent/client.py">purge_user_memory</a>(...) -> MemoryDeleteResponse</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Delete all memories for one external application user.
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```python
from general_augment import GeneralAugmentClient
from general_augment.environment import GeneralAugmentClientEnvironment

client = GeneralAugmentClient(
    admin_key="<value>",
    environment=GeneralAugmentClientEnvironment.PRODUCTION,
)

client.agent.purge_user_memory(
    user_id="user_id",
)

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**user_id:** `str` 
    
</dd>
</dl>

<dl>
<dd>

**agent_run_id:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**request_options:** `typing.Optional[RequestOptions]` — Request-specific configuration.
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.agent.<a href="src/general_augment/agent/client.py">delete_memory</a>(...) -> MemoryDeleteResponse</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Delete one memory fact for one external application user.
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```python
from general_augment import GeneralAugmentClient
from general_augment.environment import GeneralAugmentClientEnvironment

client = GeneralAugmentClient(
    admin_key="<value>",
    environment=GeneralAugmentClientEnvironment.PRODUCTION,
)

client.agent.delete_memory(
    memory_id="memory_id",
    user_id="user_id",
)

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**memory_id:** `str` 
    
</dd>
</dl>

<dl>
<dd>

**user_id:** `str` 
    
</dd>
</dl>

<dl>
<dd>

**agent_run_id:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**request_options:** `typing.Optional[RequestOptions]` — Request-specific configuration.
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

## Responses
<details><summary><code>client.responses.<a href="src/general_augment/responses/client.py">create_response</a>(...) -> ResponsesResponse</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Create one Responses API-compatible agent response.
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```python
from general_augment import GeneralAugmentClient
from general_augment.environment import GeneralAugmentClientEnvironment

client = GeneralAugmentClient(
    admin_key="<value>",
    environment=GeneralAugmentClientEnvironment.PRODUCTION,
)

client.responses.create_response(
    model="model",
)

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**model:** `str` 
    
</dd>
</dl>

<dl>
<dd>

**agent_id:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**deployment_id:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**agent:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**background:** `typing.Optional[bool]` 
    
</dd>
</dl>

<dl>
<dd>

**conversation:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**include:** `typing.Optional[typing.List[str]]` 
    
</dd>
</dl>

<dl>
<dd>

**input:** `typing.Optional[typing.Any]` 
    
</dd>
</dl>

<dl>
<dd>

**instructions:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**max_output_tokens:** `typing.Optional[int]` 
    
</dd>
</dl>

<dl>
<dd>

**metadata:** `typing.Optional[typing.Dict[str, typing.Any]]` 
    
</dd>
</dl>

<dl>
<dd>

**parallel_tool_calls:** `typing.Optional[bool]` 
    
</dd>
</dl>

<dl>
<dd>

**previous_response_id:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**reasoning:** `typing.Optional[ResponsesReasoning]` 
    
</dd>
</dl>

<dl>
<dd>

**reasoning_effort:** `typing.Optional[ResponsesRequestReasoningEffort]` 
    
</dd>
</dl>

<dl>
<dd>

**service_tier:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**store:** `typing.Optional[bool]` 
    
</dd>
</dl>

<dl>
<dd>

**stream:** `typing.Optional[bool]` 
    
</dd>
</dl>

<dl>
<dd>

**temperature:** `typing.Optional[float]` 
    
</dd>
</dl>

<dl>
<dd>

**text:** `typing.Optional[typing.Dict[str, typing.Any]]` 
    
</dd>
</dl>

<dl>
<dd>

**tool_choice:** `typing.Optional[typing.Any]` 
    
</dd>
</dl>

<dl>
<dd>

**tools:** `typing.Optional[typing.List[typing.Dict[str, typing.Any]]]` 
    
</dd>
</dl>

<dl>
<dd>

**top_p:** `typing.Optional[float]` 
    
</dd>
</dl>

<dl>
<dd>

**truncation:** `typing.Optional[ResponsesRequestTruncation]` 
    
</dd>
</dl>

<dl>
<dd>

**user:** `typing.Optional[str]` 
    
</dd>
</dl>

<dl>
<dd>

**request_options:** `typing.Optional[RequestOptions]` — Request-specific configuration.
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

