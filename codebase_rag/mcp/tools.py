import asyncio
import itertools
from pathlib import Path

from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import logs as lg
from codebase_rag import tool_errors as te
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.models import ToolMetadata
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.services.llm import CypherGenerator, _validate_cypher_read_only
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.tools.code_retrieval import CodeRetriever, create_code_retrieval_tool
from codebase_rag.tools.codebase_query import create_query_tool
from codebase_rag.tools.directory_lister import (
    DirectoryLister,
    create_directory_lister_tool,
)
from codebase_rag.tools.file_editor import FileEditor, create_file_editor_tool
from codebase_rag.tools.file_reader import FileReader, create_file_reader_tool
from codebase_rag.tools.file_writer import FileWriter, create_file_writer_tool
from codebase_rag.types_defs import (
    CodeSnippetResultDict,
    DeleteProjectErrorResult,
    DeleteProjectResult,
    DeleteProjectSuccessResult,
    ListProjectsErrorResult,
    ListProjectsResult,
    ListProjectsSuccessResult,
    MCPHandlerType,
    MCPInputSchema,
    MCPInputSchemaProperty,
    MCPToolSchema,
    QueryResultDict,
)
from codebase_rag.vector_store import delete_project_embeddings


class MCPToolsRegistry:
    def __init__(
        self,
        project_root: str,
        ingestor: MemgraphIngestor,
        cypher_gen: CypherGenerator | None = None,
    ) -> None:
        self.project_root = project_root
        self.ingestor = ingestor
        self.cypher_gen = cypher_gen
        self._ingestor_lock = asyncio.Lock()

        self.parsers, self.queries = load_parsers()

        self.code_retriever = CodeRetriever(project_root, ingestor)
        self.file_editor = FileEditor(project_root=project_root)
        self.file_reader = FileReader(project_root=project_root)
        self.file_writer = FileWriter(project_root=project_root)
        self.directory_lister = DirectoryLister(project_root=project_root)

        self._project_root_path = Path(project_root).resolve()

        if cypher_gen is not None:
            self._query_tool = create_query_tool(
                ingestor=ingestor, cypher_gen=cypher_gen, console=None
            )
        else:
            self._query_tool = None
        self._code_tool = create_code_retrieval_tool(code_retriever=self.code_retriever)
        self._file_editor_tool = create_file_editor_tool(file_editor=self.file_editor)
        self._file_reader_tool = create_file_reader_tool(file_reader=self.file_reader)
        self._file_writer_tool = create_file_writer_tool(file_writer=self.file_writer)
        self._directory_lister_tool = create_directory_lister_tool(
            directory_lister=self.directory_lister
        )

        self._tools: dict[str, ToolMetadata] = {
            cs.MCPToolName.LIST_PROJECTS: ToolMetadata(
                name=cs.MCPToolName.LIST_PROJECTS,
                description=td.MCP_TOOLS[cs.MCPToolName.LIST_PROJECTS],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={},
                    required=[],
                ),
                handler=self.list_projects,
                returns_json=True,
            ),
            cs.MCPToolName.DELETE_PROJECT: ToolMetadata(
                name=cs.MCPToolName.DELETE_PROJECT,
                description=td.MCP_TOOLS[cs.MCPToolName.DELETE_PROJECT],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.PROJECT_NAME: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_PROJECT_NAME,
                        )
                    },
                    required=[cs.MCPParamName.PROJECT_NAME],
                ),
                handler=self.delete_project,
                returns_json=True,
            ),
            cs.MCPToolName.WIPE_DATABASE: ToolMetadata(
                name=cs.MCPToolName.WIPE_DATABASE,
                description=td.MCP_TOOLS[cs.MCPToolName.WIPE_DATABASE],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.CONFIRM: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.BOOLEAN,
                            description=td.MCP_PARAM_CONFIRM,
                        )
                    },
                    required=[cs.MCPParamName.CONFIRM],
                ),
                handler=self.wipe_database,
                returns_json=False,
            ),
            cs.MCPToolName.INDEX_REPOSITORY: ToolMetadata(
                name=cs.MCPToolName.INDEX_REPOSITORY,
                description=td.MCP_TOOLS[cs.MCPToolName.INDEX_REPOSITORY],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={},
                    required=[],
                ),
                handler=self.index_repository,
                returns_json=False,
            ),
            cs.MCPToolName.QUERY_CODE_GRAPH: ToolMetadata(
                name=cs.MCPToolName.QUERY_CODE_GRAPH,
                description=td.MCP_TOOLS[cs.MCPToolName.QUERY_CODE_GRAPH],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.NATURAL_LANGUAGE_QUERY: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_NATURAL_LANGUAGE_QUERY,
                        )
                    },
                    required=[cs.MCPParamName.NATURAL_LANGUAGE_QUERY],
                ),
                handler=self.query_code_graph,
                returns_json=True,
            ),
            cs.MCPToolName.GET_CODE_SNIPPET: ToolMetadata(
                name=cs.MCPToolName.GET_CODE_SNIPPET,
                description=td.MCP_TOOLS[cs.MCPToolName.GET_CODE_SNIPPET],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.QUALIFIED_NAME: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_QUALIFIED_NAME,
                        )
                    },
                    required=[cs.MCPParamName.QUALIFIED_NAME],
                ),
                handler=self.get_code_snippet,
                returns_json=True,
            ),
            cs.MCPToolName.SURGICAL_REPLACE_CODE: ToolMetadata(
                name=cs.MCPToolName.SURGICAL_REPLACE_CODE,
                description=td.MCP_TOOLS[cs.MCPToolName.SURGICAL_REPLACE_CODE],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.FILE_PATH: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_FILE_PATH,
                        ),
                        cs.MCPParamName.TARGET_CODE: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_TARGET_CODE,
                        ),
                        cs.MCPParamName.REPLACEMENT_CODE: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_REPLACEMENT_CODE,
                        ),
                    },
                    required=[
                        cs.MCPParamName.FILE_PATH,
                        cs.MCPParamName.TARGET_CODE,
                        cs.MCPParamName.REPLACEMENT_CODE,
                    ],
                ),
                handler=self.surgical_replace_code,
                returns_json=False,
            ),
            cs.MCPToolName.READ_FILE: ToolMetadata(
                name=cs.MCPToolName.READ_FILE,
                description=td.MCP_TOOLS[cs.MCPToolName.READ_FILE],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.FILE_PATH: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_FILE_PATH,
                        ),
                        cs.MCPParamName.OFFSET: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.INTEGER,
                            description=td.MCP_PARAM_OFFSET,
                        ),
                        cs.MCPParamName.LIMIT: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.INTEGER,
                            description=td.MCP_PARAM_LIMIT,
                        ),
                    },
                    required=[cs.MCPParamName.FILE_PATH],
                ),
                handler=self.read_file,
                returns_json=False,
            ),
            cs.MCPToolName.WRITE_FILE: ToolMetadata(
                name=cs.MCPToolName.WRITE_FILE,
                description=td.MCP_TOOLS[cs.MCPToolName.WRITE_FILE],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.FILE_PATH: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_FILE_PATH,
                        ),
                        cs.MCPParamName.CONTENT: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_CONTENT,
                        ),
                    },
                    required=[
                        cs.MCPParamName.FILE_PATH,
                        cs.MCPParamName.CONTENT,
                    ],
                ),
                handler=self.write_file,
                returns_json=False,
            ),
            cs.MCPToolName.LIST_DIRECTORY: ToolMetadata(
                name=cs.MCPToolName.LIST_DIRECTORY,
                description=td.MCP_TOOLS[cs.MCPToolName.LIST_DIRECTORY],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.DIRECTORY_PATH: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_DIRECTORY_PATH,
                            default=cs.MCP_DEFAULT_DIRECTORY,
                        )
                    },
                    required=[],
                ),
                handler=self.list_directory,
                returns_json=False,
            ),
            cs.MCPToolName.RUN_CYPHER: ToolMetadata(
                name=cs.MCPToolName.RUN_CYPHER,
                description=td.MCP_TOOLS[cs.MCPToolName.RUN_CYPHER],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={
                        cs.MCPParamName.CYPHER_QUERY: MCPInputSchemaProperty(
                            type=cs.MCPSchemaType.STRING,
                            description=td.MCP_PARAM_CYPHER_QUERY,
                        )
                    },
                    required=[cs.MCPParamName.CYPHER_QUERY],
                ),
                handler=self.run_cypher,
                returns_json=True,
            ),
            cs.MCPToolName.GET_GRAPH_SCHEMA: ToolMetadata(
                name=cs.MCPToolName.GET_GRAPH_SCHEMA,
                description=td.MCP_TOOLS[cs.MCPToolName.GET_GRAPH_SCHEMA],
                input_schema=MCPInputSchema(
                    type=cs.MCPSchemaType.OBJECT,
                    properties={},
                    required=[],
                ),
                handler=self.get_graph_schema,
                returns_json=True,
            ),
        }

    async def list_projects(self) -> ListProjectsResult:
        logger.info(lg.MCP_LISTING_PROJECTS)
        try:
            projects = await asyncio.to_thread(self.ingestor.list_projects)
            return ListProjectsSuccessResult(projects=projects, count=len(projects))
        except Exception as e:
            logger.error(lg.MCP_ERROR_LIST_PROJECTS.format(error=e))
            return ListProjectsErrorResult(error=str(e), projects=[], count=0)

    def _get_project_node_ids(self, project_name: str) -> list[int]:
        rows = self.ingestor.fetch_all(
            cs.CYPHER_QUERY_PROJECT_NODE_IDS,
            {cs.KEY_PROJECT_NAME: project_name},
        )
        result: list[int] = []
        for row in rows:
            node_id = row.get(cs.KEY_NODE_ID)
            if isinstance(node_id, int):
                result.append(node_id)
        return result

    def _cleanup_project_embeddings(self, project_name: str) -> None:
        node_ids = self._get_project_node_ids(project_name)
        delete_project_embeddings(project_name, node_ids)

    def _delete_project_sync(self, project_name: str) -> DeleteProjectResult:
        projects = self.ingestor.list_projects()
        if project_name not in projects:
            return DeleteProjectErrorResult(
                success=False,
                error=te.MCP_PROJECT_NOT_FOUND.format(
                    project_name=project_name, projects=projects
                ),
            )
        self._cleanup_project_embeddings(project_name)
        self.ingestor.delete_project(project_name)
        return DeleteProjectSuccessResult(
            success=True,
            project=project_name,
            message=cs.MCP_PROJECT_DELETED.format(project_name=project_name),
        )

    async def delete_project(self, project_name: str) -> DeleteProjectResult:
        logger.info(lg.MCP_DELETING_PROJECT.format(project_name=project_name))
        try:
            async with self._ingestor_lock:
                return await asyncio.to_thread(self._delete_project_sync, project_name)
        except Exception as e:
            logger.error(lg.MCP_ERROR_DELETE_PROJECT.format(error=e))
            return DeleteProjectErrorResult(success=False, error=str(e))

    async def wipe_database(self, confirm: bool) -> str:
        if not confirm:
            return cs.MCP_WIPE_CANCELLED
        logger.warning(lg.MCP_WIPING_DATABASE)
        try:
            async with self._ingestor_lock:
                await asyncio.to_thread(self.ingestor.clean_database)
            return cs.MCP_WIPE_SUCCESS
        except Exception as e:
            logger.error(lg.MCP_ERROR_WIPE.format(error=e))
            return cs.MCP_WIPE_ERROR.format(error=e)

    def _index_repository_sync(self) -> str:
        project_name = Path(self.project_root).resolve().name
        logger.info(lg.MCP_CLEARING_PROJECT.format(project_name=project_name))
        self._cleanup_project_embeddings(project_name)
        self.ingestor.delete_project(project_name)

        updater = GraphUpdater(
            ingestor=self.ingestor,
            repo_path=Path(self.project_root),
            parsers=self.parsers,
            queries=self.queries,
        )
        updater.run()

        return cs.MCP_INDEX_SUCCESS_PROJECT.format(
            path=self.project_root, project_name=project_name
        )

    async def index_repository(self) -> str:
        logger.info(lg.MCP_INDEXING_REPO.format(path=self.project_root))
        try:
            async with self._ingestor_lock:
                return await asyncio.to_thread(self._index_repository_sync)
        except Exception as e:
            logger.error(lg.MCP_ERROR_INDEXING.format(error=e))
            return cs.MCP_INDEX_ERROR.format(error=e)

    async def query_code_graph(self, natural_language_query: str) -> QueryResultDict:
        logger.info(lg.MCP_QUERY_CODE_GRAPH.format(query=natural_language_query))
        if self._query_tool is None:
            return QueryResultDict(
                error="query_code_graph requires an LLM provider. Use run_cypher instead.",
                query_used=cs.QUERY_NOT_AVAILABLE,
                results=[],
                summary="No LLM provider configured. Use get_graph_schema + run_cypher to query directly.",
            )
        try:
            graph_data = await self._query_tool.function(natural_language_query)
            result_dict: QueryResultDict = graph_data.model_dump()
            logger.info(
                lg.MCP_QUERY_RESULTS.format(
                    count=len(result_dict.get(cs.DICT_KEY_RESULTS, []))
                )
            )
            return result_dict
        except Exception as e:
            logger.exception(lg.MCP_ERROR_QUERY.format(error=e))
            return QueryResultDict(
                error=str(e),
                query_used=cs.QUERY_NOT_AVAILABLE,
                results=[],
                summary=cs.MCP_TOOL_EXEC_ERROR.format(
                    name=cs.MCPToolName.QUERY_CODE_GRAPH, error=e
                ),
            )

    async def get_code_snippet(self, qualified_name: str) -> CodeSnippetResultDict:
        logger.info(lg.MCP_GET_CODE_SNIPPET.format(name=qualified_name))
        try:
            snippet = await self._code_tool.function(qualified_name=qualified_name)
            result: CodeSnippetResultDict | None = snippet.model_dump()
            if result is None:
                return CodeSnippetResultDict(
                    error=te.MCP_TOOL_RETURNED_NONE,
                    found=False,
                    error_message=te.MCP_INVALID_RESPONSE,
                )
            return result
        except Exception as e:
            logger.error(lg.MCP_ERROR_CODE_SNIPPET.format(error=e))
            return CodeSnippetResultDict(
                error=str(e),
                found=False,
                error_message=str(e),
            )

    async def surgical_replace_code(
        self, file_path: str, target_code: str, replacement_code: str
    ) -> str:
        logger.info(lg.MCP_SURGICAL_REPLACE.format(path=file_path))
        try:
            resolved = self._resolve_path(file_path)
            try:
                resolved_rel = str(resolved.relative_to(self._project_root_path))
            except ValueError:
                resolved_rel = file_path
            result = await self._file_editor_tool.function(
                file_path=resolved_rel,
                target_code=target_code,
                replacement_code=replacement_code,
            )
            return str(result)
        except Exception as e:
            logger.error(lg.MCP_ERROR_REPLACE.format(error=e))
            return te.ERROR_WRAPPER.format(message=e)

    def _resolve_path(self, file_path: str) -> Path:
        """Resolve a file path, searching repo subdirectories if needed.

        In multi-repo setups, file paths in the graph are relative to
        individual repo roots (e.g. ``src/main.py``), but project_root
        points to the profile directory containing multiple repos.
        When the direct path doesn't exist, try prepending each
        immediate subdirectory name (the repo folders).
        """
        direct = self._project_root_path / file_path
        if direct.exists():
            return direct
        for child in sorted(self._project_root_path.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                candidate = child / file_path
                if candidate.exists():
                    return candidate
        return direct  # fall back so callers get a sensible error

    async def read_file(
        self, file_path: str, offset: int | None = None, limit: int | None = None
    ) -> str:
        logger.info(lg.MCP_READ_FILE.format(path=file_path, offset=offset, limit=limit))
        try:
            resolved = self._resolve_path(file_path)
            # Convert back to a path relative to project_root for the
            # file-reader tool (which applies its own validation).
            try:
                resolved_rel = str(resolved.relative_to(self._project_root_path))
            except ValueError:
                resolved_rel = file_path

            if offset is not None or limit is not None:
                full_path = resolved
                start = offset if offset is not None else 0

                with open(full_path, encoding=cs.ENCODING_UTF8) as f:
                    skipped_count = sum(1 for _ in itertools.islice(f, start))

                    if limit is not None:
                        sliced_lines = [line for _, line in zip(range(limit), f)]
                    else:
                        sliced_lines = list(f)

                    paginated_content = "".join(sliced_lines)

                    remaining_lines_count = sum(1 for _ in f)
                    total_lines = (
                        skipped_count + len(sliced_lines) + remaining_lines_count
                    )

                    header = cs.MCP_PAGINATION_HEADER.format(
                        start=start + 1,
                        end=start + len(sliced_lines),
                        total=total_lines,
                    )
                    return header + paginated_content
            else:
                result = await self._file_reader_tool.function(file_path=resolved_rel)
                return str(result)

        except Exception as e:
            logger.error(lg.MCP_ERROR_READ.format(error=e))
            return te.ERROR_WRAPPER.format(message=e)

    async def write_file(self, file_path: str, content: str) -> str:
        logger.info(lg.MCP_WRITE_FILE.format(path=file_path))
        try:
            resolved = self._resolve_path(file_path)
            try:
                resolved_rel = str(resolved.relative_to(self._project_root_path))
            except ValueError:
                resolved_rel = file_path
            result = await self._file_writer_tool.function(
                file_path=resolved_rel, content=content
            )
            if result.success:
                return cs.MCP_WRITE_SUCCESS.format(path=file_path)
            return te.ERROR_WRAPPER.format(message=result.error_message)
        except Exception as e:
            logger.error(lg.MCP_ERROR_WRITE.format(error=e))
            return te.ERROR_WRAPPER.format(message=e)

    async def list_directory(
        self, directory_path: str = cs.MCP_DEFAULT_DIRECTORY
    ) -> str:
        logger.info(lg.MCP_LIST_DIR.format(path=directory_path))
        try:
            resolved = self._resolve_path(directory_path)
            try:
                resolved_rel = str(resolved.relative_to(self._project_root_path))
            except ValueError:
                resolved_rel = directory_path
            result = self._directory_lister_tool.function(directory_path=resolved_rel)
            return str(result)
        except Exception as e:
            logger.error(lg.MCP_ERROR_LIST_DIR.format(error=e))
            return te.ERROR_WRAPPER.format(message=e)

    async def run_cypher(self, cypher_query: str) -> dict:
        logger.info(f"MCP run_cypher: {cypher_query}")
        try:
            _validate_cypher_read_only(cypher_query)
            rows = await asyncio.to_thread(self.ingestor.fetch_all, cypher_query)
            return {"results": rows, "count": len(rows)}
        except Exception as e:
            logger.error(f"MCP run_cypher error: {e}")
            return {"error": str(e), "results": [], "count": 0}

    async def get_graph_schema(self) -> dict:
        logger.info("MCP get_graph_schema")
        try:
            labels_rows = await asyncio.to_thread(
                self.ingestor.fetch_all, "CALL schema.node_type_properties() YIELD nodeType, propertyName RETURN nodeType, collect(propertyName) as properties;"
            )
            rel_rows = await asyncio.to_thread(
                self.ingestor.fetch_all, "CALL schema.rel_type_properties() YIELD relType RETURN DISTINCT relType;"
            )
            return {
                "node_types": labels_rows,
                "relationship_types": rel_rows,
            }
        except Exception as e:
            logger.error(f"MCP get_graph_schema error: {e}")
            return {"error": str(e)}

    def get_tool_schemas(self) -> list[MCPToolSchema]:
        return [
            MCPToolSchema(
                name=metadata.name,
                description=metadata.description,
                inputSchema=metadata.input_schema,
            )
            for metadata in self._tools.values()
        ]

    def get_tool_handler(self, name: str) -> tuple[MCPHandlerType, bool] | None:
        metadata = self._tools.get(name)
        return None if metadata is None else (metadata.handler, metadata.returns_json)


def create_mcp_tools_registry(
    project_root: str,
    ingestor: MemgraphIngestor,
    cypher_gen: CypherGenerator | None = None,
) -> MCPToolsRegistry:
    return MCPToolsRegistry(
        project_root=project_root,
        ingestor=ingestor,
        cypher_gen=cypher_gen,
    )
