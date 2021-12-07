import inspect
import os
from contextvars import copy_context
from typing import Mapping, Optional, Union

from argo_workflow_tools.dsl import building_mode_context, workflow_template_collector
from argo_workflow_tools.dsl.condition import BinaryOp, UnaryOp
from argo_workflow_tools.dsl.dag_task import DAGReference, NodeReference, TaskReference
from argo_workflow_tools.dsl.node import DAGNode
from argo_workflow_tools.dsl.input_definition import InputDefinition, SourceType
from argo_workflow_tools.dsl.node_properties import (
    DAGNodeProperties,
    TaskNodeProperties,
)
from argo_workflow_tools.dsl.parameter_builders.json_parameter_builder import (
    JSONParameterBuilder,
)

from argo_workflow_tools.dsl.utils.utils import (
    get_arguments,
    get_inputs,
    get_outputs,
    sanitize_name,
)
from argo_workflow_tools.models.io.argoproj.workflow import v1alpha1 as argo


def _create_task_script(
    func_obj: TaskReference, parameters: dict[str, argo.Parameter]
) -> str:
    """
    generates a runnable script out of a task function, adding input and output boilerplate
    Parameters
    ----------
    func_obj : task function

    Returns
    -------
    runnable script
    """
    if func_obj.func is None:
        return None
    code = inspect.getsource(func_obj.func)
    code = code[code.find("def ") :]
    builder_imports = set()
    inputs = ""
    for name, argument in func_obj.arguments.items():
        json_parameter = JSONParameterBuilder(name, parameters[name].name)
        builder_imports = builder_imports.union(json_parameter.imports())
        inputs += json_parameter.variable_from_input() + os.linesep
    builder = func_obj.outputs.parameter_builder
    outputs = builder.variable_to_output()
    call = f"result={func_obj.func.__name__}({str.join(',', inspect.signature(func_obj.func).parameters.keys())})"
    builder_imports = str.join(
        os.linesep, list(builder_imports.union(builder.imports()))
    )
    script = (
        f"{builder_imports}\n"
        + f"{code}\n"
        + f"{inputs}\n"
        + f"{call}\n"
        + f"{outputs}"
    )
    return script


def _fill_task_metadata(task_template: argo.Template, properties: TaskNodeProperties):
    task_template.retry_strategy = properties.retry_strategy
    task_template.parallelism = properties.parallelism
    task_template.fail_fast = properties.fail_fast
    task_template.active_deadline_seconds = properties.active_deadline_seconds
    task_template.affinity = properties.affinity
    task_template.tolerations = properties.tolerations
    task_template.node_selector = properties.node_selector
    task_template.metadata = argo.Metadata(
        labels=properties.labels, annotations=properties.annotations
    )
    task_template.service_account_name = properties.service_account_name
    task_template.script.resources = properties.resources
    task_template.script.image_pull_policy = properties.image_pull_policy
    task_template.script.env_from = properties.env_from
    task_template.script.env = properties.env
    task_template.script.working_dir = properties.working_dir
    return task_template


def _fill_dag_metadata(task_template: argo.Template, properties: DAGNodeProperties):
    """
    fills DAG template with Argo specific metadata
    Parameters
    ----------
    task_template : base Argo template to add tasks into
    properties : DAGNodeProperties to add to the template

    Returns
    -------
    filled task_template
    """
    task_template.retry_strategy = properties.retry_strategy
    task_template.parallelism = properties.parallelism
    task_template.fail_fast = properties.fail_fast
    task_template.active_deadline_seconds = properties.active_deadline_seconds
    task_template.metadata = argo.Metadata(
        labels=properties.labels, annotations=properties.annotations
    )
    task_template.dag.fail_fast = properties.fail_fast
    return task_template


def _build_with(params: list[InputDefinition]) -> Optional[str]:
    """
    builds "with" param for loop DAGs by analyzing the inputs and looking iterable inputs.
    Parameters
    ----------
    params : list of inputs

    Returns
    -------
    "with" string
    """
    if any(param.is_partition for param in params):
        param = next(x for x in params if x.is_partition)
        return _build_node_input("param", param.partition_source).value
    else:
        return None


def build_condition(conditions: list[Union[BinaryOp, UnaryOp]]):
    if not conditions or len(conditions) == 0:
        return None

    condition_expr = [condition for condition in conditions]

    return "&&".join(condition_expr)


def _build_dag_task(dag_task: NodeReference) -> argo.DagTask:
    dependencies = set(
        filter(
            lambda dependency: dependency,
            [
                input_dep.source_node_id
                for input_dep in filter(
                    lambda x: isinstance(x, InputDefinition)
                    and not x.source_type == SourceType.PARAMETER,
                    list(dag_task.arguments.values()) + dag_task.wait_for,
                )
            ],
        )
    )
    with_param = _build_with(dag_task.arguments.values())
    arguments = [
        _build_node_input(input_name, input_type)
        for input_name, input_type in dag_task.arguments.items()
    ]

    if isinstance(dag_task, DAGReference):
        dag = _build_dag_template(dag_task.node)

        return argo.DagTask(
            name=dag_task.id,
            template=dag.name,
            arguments=get_arguments(list(arguments)),
            dependencies=list(dependencies),
            withParam=with_param,
            when=build_condition(dag_task.conditions),
        )
    elif isinstance(dag_task, TaskReference):
        task_template = _build_task_template(dag_task)

        task = argo.DagTask(
            name=dag_task.id,
            template=task_template.name,
            dependencies=list(dependencies),
            arguments=get_arguments(arguments),
            withParam=with_param,
            when=build_condition(dag_task.conditions),
        )
        return task
    else:
        raise AssertionError("only DAG or task nodes are supported")


def _build_node_input(input_name: str, input_def: InputDefinition) -> argo.Parameter:
    return argo.Parameter(name=sanitize_name(input_name), value=input_def.path())


def _build_input_parameter(parameter: InputDefinition) -> argo.Parameter:
    """
    Builds Argo Parameter out of InputDefinition
    """
    if parameter.source_type == SourceType.PARAMETER:
        argo_parameter = argo.Parameter(name=sanitize_name(parameter.name))
        return argo_parameter
    else:
        argo_parameter = argo.Parameter(name=parameter.name, value=parameter.path())
        return argo_parameter


def _build_dag_outputs(
    dag_output: Union[
        None,
        InputDefinition,
        Mapping[str, InputDefinition],
    ]
) -> list[Union[argo.Parameter, argo.Artifact]]:
    """
    Builds DAG output parameter out of DAG definition
    """
    outputs: Mapping[str, InputDefinition] = {}

    if (
        isinstance(dag_output, InputDefinition)
        and dag_output.source_type == SourceType.NODE_OUTPUT
    ):
        outputs = {"result": dag_output}
    elif isinstance(dag_output, Mapping):
        outputs = dag_output
    elif dag_output is not None:
        raise TypeError(
            f"This DAG returned a value of type [{type(dag_output).__name__}]."
            f"DAG's may only return a result of a nested DAG or Task"
        )

    return [
        argo.Parameter(
            name=output.name,
            valueFrom=argo.ValueFrom(parameter=output.path()),
        )
        for output_name, output in outputs.items()
    ]


def _build_task_template(task_node: TaskReference) -> argo.Template:
    """
    Builds an Argo Script Template out of a TaskNode
    Parameters
    ----------
    task_node : TaskNode to parse into a Script Template

    Returns
    -------
    Argo Script Template

    """
    parameters = {
        param_name: InputDefinition(
            source_type=SourceType.PARAMETER, name=sanitize_name(param_name)
        )
        for param_name in inspect.signature(task_node.func).parameters
    }

    task_inputs = {
        input_name: _build_input_parameter(input_definition)
        for input_name, input_definition in parameters.items()
    }
    source = _create_task_script(task_node, task_inputs)
    output = argo.Parameter(
        name="result",
        valueFrom=argo.ValueFrom(
            path=task_node.outputs.parameter_builder.artifact_path
        ),
    )
    task_outputs = get_inputs([output])

    task_template = argo.Template(
        name=sanitize_name(task_node.func.__name__),
        inputs=get_inputs(list(task_inputs.values())),
        script=argo.ScriptTemplate(
            image=task_node.properties.image, source=source, command=["python"]
        ),
        outputs=task_outputs,
    )

    task_template = _fill_task_metadata(task_template, task_node.properties)

    workflow_template_collector.add_template(task_template)

    return task_template


def _build_dag_template(node: DAGNode) -> argo.Template:
    """
    Builds an Argo DAG Template out of a DAGNode
    Parameters
    ----------
    node : DAGNode to parse into a DAG Template

    Returns
    -------
    Argo DAG Template

    """
    parameters = {
        param_name: InputDefinition(
            source_type=SourceType.PARAMETER, name=sanitize_name(param_name)
        )
        for param_name in inspect.signature(node.func).parameters
    }
    ctx = copy_context()
    dag_output = ctx.run(node.func, **parameters)
    dag_tasks = ctx.get(workflow_template_collector.dag_tasks, [])

    dag_inputs = [
        _build_input_parameter(input_type)
        for input_name, input_type in parameters.items()
    ]

    dag_outputs = _build_dag_outputs(dag_output)

    dag_tasks = [_build_dag_task(dag_task) for dag_task in dag_tasks]

    dag_tamplate = argo.Template(
        dag=argo.DagTemplate(tasks=list(dag_tasks)),
        name=sanitize_name(node.func.__name__),
        outputs=get_outputs(dag_outputs),
        inputs=get_inputs(dag_inputs),
    )

    dag_tamplate = _fill_dag_metadata(dag_tamplate, node.properties)

    workflow_template_collector.add_template(dag_tamplate)
    return dag_tamplate


def compile_dag(entrypoint: DAGNode) -> argo.WorkflowSpec:
    """
    compiles a DAG annotated function into a WorkflowSpec arg model
    Parameters
    ----------
    entrypoint : DAG entrypoint

    Returns
    -------
    WorkflowSpec of the generated DAG
    """
    token = building_mode_context.dag_building_mode.set(True)
    try:
        if not isinstance(entrypoint, DAGNode):
            raise ValueError(
                f"{entrypoint.__name__} is not decorated with DAG or Task decorator"
            )
        result = _build_dag_template(entrypoint)
        workflow_templates = workflow_template_collector.collect_templates()
        workflowspec = argo.WorkflowSpec(
            templates=workflow_templates, entrypoint=result.name
        )

        return workflowspec
    finally:
        building_mode_context.dag_building_mode.reset(token)
        workflow_template_collector.clear()
