import re
import os
from typing import Optional, Dict

import numpy as np
import pandas as pd

from langchain.llms import OpenAI
from langchain.chat_models import ChatOpenAI  # GPT-4 fails to follow the output langchain requires, avoid using for now
from langchain.agents import initialize_agent, load_tools, Tool, create_sql_agent
from langchain.prompts import PromptTemplate
from langchain.utilities import GoogleSerperAPIWrapper
from langchain.agents.agent_toolkits import SQLDatabaseToolkit
from langchain.chains.conversation.memory import ConversationSummaryBufferMemory

from mindsdb.integrations.handlers.openai_handler.openai_handler import OpenAIHandler, CHAT_MODELS
from mindsdb.integrations.handlers.langchain_handler.mindsdb_database_agent import MindsDBSQL
from mindsdb_sql import parse_sql, Insert


_DEFAULT_MODEL = 'gpt-3.5-turbo'  # TODO: enable other LLM backends (AI21, Anthropic, etc.)
_DEFAULT_MAX_TOKENS = 2048  # requires more than vanilla OpenAI due to ongoing summarization and 3rd party input
_DEFAULT_AGENT_MODEL = 'zero-shot-react-description'
_DEFAULT_AGENT_TOOLS = ['python_repl', 'wikipedia']  # these require no additional arguments


class LangChainHandler(OpenAIHandler):
    """
    This is a MindsDB integration for the LangChain library, which provides a unified interface for interacting with
    various large language models (LLMs).

    Currently, this integration supports exposing OpenAI's LLMs with normal text completion support. They are then
    wrapped in a zero shot react description agent that offers a few third party tools out of the box, with support
    for additional ones if an API key is provided. Ongoing memory is also provided.

    Full tool support list:
        - wikipedia
        - python_repl
        - serper.dev search

    This integration inherits from the OpenAI engine, so it shares a lot of the requirements, features (e.g. prompt
    templating) and limitations.
    """
    name = 'langchain'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stops = []
        self.default_model = _DEFAULT_MODEL
        self.default_max_tokens = _DEFAULT_MAX_TOKENS
        self.default_agent_model = _DEFAULT_AGENT_MODEL
        self.default_agent_tools = _DEFAULT_AGENT_TOOLS
        self.write_privileges = False  # if True, this agent is able to write into other active mindsdb integrations

    def _get_serper_api_key(self, args, strict=True):
        if 'serper_api_key' in args:
            return args['serper_api_key']
        # 2
        connection_args = self.engine_storage.get_connection_args()
        if 'serper_api_key' in connection_args:
            return connection_args['serper_api_key']
        # 3
        api_key = os.getenv('SERPER_API_KEY')  # e.g. "OPENAI_API_KEY"
        if api_key is not None:
            return api_key

        if strict:
            raise Exception(f'Missing API key serper_api_key. Either re-create this ML_ENGINE specifying the `serper_api_key` parameter,\
                 or re-create this model and pass the API key with `USING` syntax.')  # noqa

    def create(self, target, args=None, **kwargs):
        self.write_privileges = args.get('using', {}).get('writer', self.write_privileges)
        self.default_agent_tools = args.get('tools', self.default_agent_tools)
        super().create(target, args, **kwargs)

    @staticmethod
    def create_validation(target, args=None, **kwargs):
        if 'using' not in args:
            raise Exception("LangChain engine requires a USING clause! Refer to its documentation for more details.")
        else:
            args = args['using']

        if len(set(args.keys()) & {'prompt_template'}) == 0:
            raise Exception('Please provide a `prompt_template` for this engine.')

    def predict(self, df, args=None):
        """
        Dispatch is performed depending on the underlying model type. Currently, only the default text completion
        is supported.
        """
        executor = args['executor']  # used as tool in custom tool for the agent to have mindsdb-wide access
        pred_args = args['predict_params'] if args else {}
        args = self.model_storage.json_get('args')
        args['executor'] = executor

        df = df.reset_index(drop=True)

        if 'prompt_template' not in args and 'prompt_template' not in pred_args:
            raise Exception(f"This model expects a prompt template, please provide one.")

        if 'stops' in pred_args:
            self.stops = pred_args['stops']

        # TODO: offload creation to the `create` method instead for faster inference?
        modal_dispatch = {
            'default': 'default_completion',
            'sql_agent': 'sql_agent_completion',
        }

        agent_creation_method = modal_dispatch.get(args.get('modal_dispatch', 'default'), 'default_completion')
        agent = getattr(self, agent_creation_method)(df, args, pred_args)
        return self.run_agent(df, agent, args, pred_args)

    def default_completion(self, df, args=None, pred_args=None):
        """
        Mostly follows the logic of the OpenAI handler, but with a few additions:
            - setup the langchain toolkit
            - setup the langchain agent (memory included)
            - setup information to be published when describing the model

        Ref link from the LangChain documentation on how to accomplish the first two items:
            - python.langchain.com/en/latest/modules/agents/agents/custom_agent.html
        """
        pred_args = pred_args if pred_args else {}

        # api argument validation
        model_name = pred_args.get('model_name', args.get('model_name', self.default_model))
        agent_name = pred_args.get('agent_name', args.get('agent_name', self.default_agent_model))

        model_kwargs = {
            'model_name': model_name,
            'temperature': min(1.0, max(0.0, pred_args.get('temperature', args.get('temperature', 0.0)))),
            'max_tokens': pred_args.get('max_tokens', args.get('max_tokens', self.default_max_tokens)),
            'top_p': pred_args.get('top_p', None),
            'frequency_penalty': pred_args.get('frequency_penalty', None),
            'presence_penalty': pred_args.get('presence_penalty', None),
            'n': pred_args.get('n', None),
            'best_of': pred_args.get('best_of', None),
            'request_timeout': pred_args.get('request_timeout', None),
            'logit_bias': pred_args.get('logit_bias', None),
            'openai_api_key': self._get_openai_api_key(args, strict=True),
            'serper_api_key': self._get_serper_api_key(args, strict=False),
        }
        model_kwargs = {k: v for k, v in model_kwargs.items() if v is not None}  # filter out None values

        # langchain tool setup
        pred_args['tools'] = args.get('tools') if 'tools' not in pred_args else pred_args.get('tools', [])
        tools = self._setup_tools(model_kwargs, pred_args, args['executor'])

        # langchain agent setup
        if model_kwargs['model_name'] in CHAT_MODELS:
            llm = ChatOpenAI(**model_kwargs)
        else:
            llm = OpenAI(**model_kwargs)
        memory = ConversationSummaryBufferMemory(llm=llm, max_token_limit=model_kwargs.get('max_tokens', None))
        agent = initialize_agent(
            tools,
            llm,
            memory=memory,
            agent=agent_name,
            max_iterations=pred_args.get('max_iterations', 3),
            verbose=pred_args.get('verbose', args.get('verbose', False)),
            handle_parsing_errors=True,
        )

        # setup model description
        description = {
            'allowed_tools': [agent.agent.allowed_tools],   # packed as list to avoid additional rows
            'agent_type': agent_name,
            'max_iterations': agent.max_iterations,
            'memory_type': memory.__class__.__name__,
        }
        description = {**description, **model_kwargs}
        description.pop('openai_api_key', None)
        self.model_storage.json_set('description', description)

        return agent

    def run_agent(self, df, agent, args, pred_args):
        # TODO abstract prompt templating into a common utility method, this is also used in vanilla OpenAI
        if pred_args.get('prompt_template', False):
            base_template = pred_args['prompt_template']  # override with predict-time template if available
        else:
            base_template = args['prompt_template']

        input_variables = []
        matches = list(re.finditer("{{(.*?)}}", base_template))

        for m in matches:
            input_variables.append(m[0].replace('{', '').replace('}', ''))

        empty_prompt_ids = np.where(df[input_variables].isna().all(axis=1).values)[0]

        base_template = base_template.replace('{{', '{').replace('}}', '}')
        prompts = []

        for i, row in df.iterrows():
            if i not in empty_prompt_ids:
                prompt = PromptTemplate(input_variables=input_variables, template=base_template)
                kwargs = {}
                for col in input_variables:
                    kwargs[col] = row[col] if row[col] is not None else ''  # add empty quote if data is missing
                prompts.append(prompt.format(**kwargs))

        def _completion(agent, prompts):
            # TODO: ensure that agent completion plus prompt match the maximum allowed by the user
            # TODO: use async API if possible for parallelized completion
            completions = []
            for prompt in prompts:
                try:
                    completions.append(agent.run(prompt))
                except Exception as e:
                    completions.append(f'agent failed with error:\n{str(e)[:50]}...')
            return [c for c in completions]

        completion = _completion(agent, prompts)

        # add null completion for empty prompts
        for i in sorted(empty_prompt_ids):
            completion.insert(i, None)

        pred_df = pd.DataFrame(completion, columns=[args['target']])

        return pred_df

    def _setup_tools(self, model_kwargs, pred_args, executor):
        def _mdb_exec_call(query: str) -> str:
            """ We define it like this to pass the executor through the closure, as custom classes don't allow custom field assignment. """  # noqa
            try:
                ast_query = parse_sql(query.strip('`'), dialect='mindsdb')
                ret = executor.execute_command(ast_query)

                data = ret.data  # list of lists
                data = '\n'.join([  # rows
                    '\t'.join(      # columns
                        str(row) if isinstance(row, str) else [str(value) for value in row]
                    ) for row in data
                ])
            except Exception as e:
                data = f"mindsdb tool failed with error:\n{str(e)}"   # let the agent know
            return data

        def _mdb_exec_metadata_call(query: str) -> str:
            try:
                parts = query.replace('`', '').split('.')
                assert 1 <= len(parts) <= 2, 'query must be in the format: `integration` or `integration.table`'

                integration = parts[0]
                integrations = executor.session.integration_controller
                handler = integrations.get_handler(integration)

                if len(parts) == 1:
                    df = handler.get_tables().data_frame
                    data = f'The integration `{integration}` has {df.shape[0]} tables: {", ".join(list(df["TABLE_NAME"].values))}'  # noqa

                if len(parts) == 2:
                    df = handler.get_tables().data_frame
                    table_name = parts[-1]
                    try:
                        table_name_col = 'TABLE_NAME' if 'TABLE_NAME' in df.columns else 'table_name'
                        mdata = df[df[table_name_col] == table_name].iloc[0].to_list()
                        if len(mdata) == 3:
                            _, nrows, table_type = mdata
                            data = f'Metadata for table {table_name}:\n\tRow count: {nrows}\n\tType: {table_type}\n'
                        elif len(mdata) == 2:
                            nrows = mdata
                            data = f'Metadata for table {table_name}:\n\tRow count: {nrows}\n'
                        else:
                            data = f'Metadata for table {table_name}:\n'
                        fields = handler.get_columns(table_name).data_frame['Field'].to_list()
                        types = handler.get_columns(table_name).data_frame['Type'].to_list()
                        data += f'List of columns and types:\n'
                        data += '\n'.join([f'\tColumn: `{field}`\tType: `{typ}`' for field, typ in zip(fields, types)])
                    except:
                        data = f'Table {table_name} not found.'
            except Exception as e:
                data = f"mindsdb tool failed with error:\n{str(e)}"  # let the agent know
            return data

        def _mdb_write_call(query: str) -> str:
            try:
                query = query.strip('`')
                ast_query = parse_sql(query.strip('`'), dialect='mindsdb')
                if isinstance(ast_query, Insert):
                    _ = executor.execute_command(ast_query)
                    return "mindsdb write tool executed successfully"
            except Exception as e:
                return f"mindsdb write tool failed with error:\n{str(e)}"

        mdb_tool = Tool(
                name="MindsDB",
                func=_mdb_exec_call,
                description="useful to read from databases or tables connected to the mindsdb machine learning package. the action must be a valid simple SQL query, always ending with a semicolon. For example, you can do `show databases;` to list the available data sources, and `show tables;` to list the available tables within each data source."  # noqa
            )

        mdb_meta_tool = Tool(
            name="MDB-Metadata",
            func=_mdb_exec_metadata_call,
            description="useful to get column names from a mindsdb table or metadata from a mindsdb data source. the command should be either 1) a data source name, to list all available tables that it exposes, or 2) a string with the format `data_source_name.table_name` (for example, `files.my_table`), to get the table name, table type, column names, data types per column, and amount of rows of the specified table."  # noqa
        )

        mdb_write_tool = Tool(
            name="MDB-Write",
            func=_mdb_write_call,
            description="useful to write into data sources connected to mindsdb. command must be a valid SQL query with syntax: `INSERT INTO data_source_name.table_name (column_name_1, column_name_2, [...]) VALUES (column_1_value_row_1, column_2_value_row_1, [...]), (column_1_value_row_2, column_2_value_row_2, [...]), [...];`. note the command always ends with a semicolon. order of column names and values for each row must be a perfect match. If write fails, try casting value with a function, passing the value without quotes, or truncating string as needed.`."  # noqa
        )

        toolkit = pred_args['tools'] if pred_args['tools'] is not None else self.default_agent_tools
        tools = load_tools(toolkit)
        if model_kwargs.get('serper_api_key', False):
            search = GoogleSerperAPIWrapper(serper_api_key=model_kwargs.pop('serper_api_key'))
            tools.append(Tool(
                name="Intermediate Answer (serper.dev)",
                func=search.run,
                description="useful for when you need to search the internet (note: in general, use this as a last resort)"  # noqa
            ))

        # add connection to mindsdb
        tools.append(mdb_tool)
        tools.append(mdb_meta_tool)

        if self.write_privileges:
            tools.append(mdb_write_tool)

        return tools

    def describe(self, attribute: Optional[str] = None) -> pd.DataFrame:
        info = self.model_storage.json_get('description')

        if attribute == 'info':
            if info is None:
                # we do this due to the huge amount of params that can be changed
                #  at prediction time to customize behavior.
                # for them, we report the last observed value
                raise Exception('This model needs to be used before it can be described.')

            description = pd.DataFrame(info)
            return description
        else:
            tables = ['info']
            return pd.DataFrame(tables, columns=['tables'])

    def finetune(self, df: Optional[pd.DataFrame] = None, args: Optional[Dict] = None) -> None:
        raise NotImplementedError('Fine-tuning is not supported for LangChain models')

    def sql_agent_completion(self, df, args=None, pred_args=None):
        """This completion will be used to answer based on information passed by any MindsDB DB or API engine."""
        db = MindsDBSQL(engine=args['executor'], metadata=args['executor'].session.integration_controller)
        toolkit = SQLDatabaseToolkit(db=db)
        model_name = args.get('model_name', self.default_model)
        llm = OpenAI(temperature=0) if model_name not in CHAT_MODELS else ChatOpenAI(temperature=0)
        agent = create_sql_agent(
            llm=llm,
            toolkit=toolkit,
            verbose=True
        )
        return agent
