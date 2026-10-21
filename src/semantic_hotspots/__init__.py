"""Offline semantic model triage for Windows, macOS and Linux."""
from .analyzer import Analysis, VERSION as __version__, analyze
from .graph import CycleError, DependencyGraph
from .model import Entity, Model, ModelError, Relationship, Table
from .parser import load_model, parse_model
from .profile import RuntimeProfile
from .simulation import SimulationBundle, SimulationConfig, simulate
from .benchmark import BenchmarkResults, collect_benchmarks, read_benchmark_csv, run_benchmarks

__all__ = ['Analysis', 'CycleError', 'DependencyGraph', 'Entity', 'Model', 'ModelError',
           'Relationship', 'RuntimeProfile', 'Table', 'analyze', 'load_model', 'parse_model',
           'SimulationBundle', 'SimulationConfig', 'simulate', 'BenchmarkResults',
           'collect_benchmarks', 'read_benchmark_csv', 'run_benchmarks',
           'LogicalNode', 'RewritePlan', 'plan_rewrites', 'RewriteResults',
           'prepare_rewrite_experiment', 'collect_rewrite_results', 'run_rewrite_experiment',
           'MatrixSpec', 'MatrixAnalysis', 'load_matrices', 'analyze_matrices',
           'ProjectExport', 'export_project', 'discover_desktop_ports', 'desktop_sessions', 'query_to_csv',
           'ShowcaseResults', 'run_showcase', 'read_performance_analyzer', 'install_adomd',
           'computation_dag', 'compare_measure_dags', 'write_dag_comparison', 'dag_to_text',
           'MeasureReview', 'review_measure']

from .optimizer import LogicalNode, RewritePlan, plan_rewrites
from .experiments import RewriteResults, prepare_rewrite_experiment, collect_rewrite_results, run_rewrite_experiment

from .matrices import MatrixSpec, MatrixAnalysis, load_matrices, analyze_matrices
from .project import ProjectExport, export_project
from .engine import discover_desktop_ports, desktop_sessions, query_to_csv
from .showcase import ShowcaseResults, run_showcase
from .performance import read_performance_analyzer
from .client_setup import install_adomd

from .comparison import computation_dag, compare_measure_dags, write_dag_comparison, dag_to_text
from .focus import MeasureReview, review_measure
