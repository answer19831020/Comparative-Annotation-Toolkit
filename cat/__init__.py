"""
Comparative Annotation Toolkit.
"""
import collections
import itertools
import logging
import multiprocessing
import os
import shutil
import json
from collections import OrderedDict
from frozendict import frozendict
from configobj import ConfigObj

import luigi
import luigi.contrib.sqla
from luigi.util import requires
from toil.job import Job
import pandas as pd
import numpy as np

import tools.bio
import tools.fileOps
import tools.intervals
import tools.gff3
import tools.hal
import tools.misc
import tools.nameConversions
import tools.procOps
import tools.mathOps
import tools.psl
import tools.sqlInterface
import tools.sqlite
import tools.hintsDatabaseInterface
import tools.transcripts
from tools.luigiAddons import multiple_requires, IndexTarget
from align_transcripts import align_transcripts
from augustus import augustus
from augustus_cgp import augustus_cgp
from augustus_pb import augustus_pb
from chaining import chaining
from classify import classify
from consensus import generate_consensus, load_alt_names
from filter_transmap import filter_transmap
from hgm import hgm, parse_hgm_gtf
from transmap_classify import transmap_classify
from plots import generate_plots
from hints_db import hints_db
from exceptions import *

logger = logging.getLogger('cat')


###
# Base tasks shared by pipeline tasks
###


class PipelineTask(luigi.Task):
    """
    Base class for all tasks in this pipeline. Has Parameters for all input parameters that will be inherited
    by all downstream tools.

    Provides useful methods for handling parameters being passed between modules.

    Note: significant here is not the same as significant in get_pipeline_args. Significant here is for the luigi
    scheduler to know which parameters define a unique task ID. This would come into play if multiple instances of this
    pipeline are being run on the same scheduler at once.
    """
    hal = luigi.Parameter()
    ref_genome = luigi.Parameter()
    config = luigi.Parameter()
    out_dir = luigi.Parameter(default='./cat_output')
    work_dir = luigi.Parameter(default='./cat_work')
    target_genomes = luigi.TupleParameter(default=None)
    # AugustusTM(R) parameters
    augustus = luigi.BoolParameter(default=False)
    augustus_species = luigi.Parameter(default='human', significant=False)
    tm_cfg = luigi.Parameter(default='augustus_cfgs/extrinsic.ETM1.cfg', significant=False)
    tmr_cfg = luigi.Parameter(default='augustus_cfgs/extrinsic.ETM2.cfg', significant=False)
    # AugustusCGP parameters
    augustus_cgp = luigi.BoolParameter(default=False)
    cgp_param = luigi.Parameter(default='augustus_cfgs/log_reg_parameters_default.cfg', significant=False)
    augustus_cgp_cfg_template = luigi.Parameter(default='augustus_cfgs/cgp_extrinsic_template.cfg', significant=False)
    maf_chunksize = luigi.IntParameter(default=2500000, significant=False)
    maf_overlap = luigi.IntParameter(default=500000, significant=False)
    # AugustusPB parameters
    augustus_pb = luigi.BoolParameter(default=False)
    pb_genome_chunksize = luigi.IntParameter(default=20000000, significant=False)
    pb_genome_overlap = luigi.IntParameter(default=500000, significant=False)
    pb_cfg = luigi.Parameter(default='augustus_cfgs/extrinsic.M.RM.PB.E.W.cfg', significant=False)
    # assemblyHub parameters
    assembly_hub = luigi.BoolParameter(default=False)
    # consensus options
    resolve_split_genes = luigi.BoolParameter(default=False, significant=False)
    intron_rnaseq_support = luigi.IntParameter(default=0, significant=False)
    exon_rnaseq_support = luigi.IntParameter(default=0, significant=False)
    intron_annot_support = luigi.IntParameter(default=0, significant=False)
    exon_annot_support = luigi.IntParameter(default=0, significant=False)
    original_intron_support = luigi.IntParameter(default=0, significant=False)
    denovo_num_introns = luigi.IntParameter(default=0, significant=False)
    denovo_splice_support = luigi.IntParameter(default=0, significant=False)
    denovo_exon_support = luigi.IntParameter(default=0, significant=False)
    require_pacbio_support = luigi.BoolParameter(default=False, significant=False)
    minimum_coverage = luigi.IntParameter(default=40, significant=False)
    in_species_rna_support_only = luigi.BoolParameter(default=False, significant=True)
    rebuild_consensus = luigi.BoolParameter(default=False, significant=True)
    # Toil options
    batchSystem = luigi.Parameter(default='singleMachine', significant=False)
    maxCores = luigi.IntParameter(default=8, significant=False)
    parasolCommand = luigi.Parameter(default=None, significant=False)
    defaultMemory = luigi.IntParameter(default=8 * 1024 ** 3, significant=False)
    disableCaching = luigi.BoolParameter(default=False, significant=False)
    workDir = luigi.Parameter(default=None, significant=False)

    def __repr__(self):
        """override the repr to make logging cleaner"""
        if hasattr(self, 'genome'):
            return 'Task: {} for {}'.format(self.__class__.__name__, self.genome)
        elif hasattr(self, 'mode'):
            return 'Task: {} for {}'.format(self.__class__.__name__, self.mode)
        else:
            return 'Task: {}'.format(self.__class__.__name__)

    def get_pipeline_args(self):
        """returns a namespace of all of the arguments to the pipeline. Resolves the target genomes variable"""
        args = tools.misc.PipelineNamespace()
        args.set('hal', os.path.abspath(self.hal), True)
        args.set('ref_genome', self.ref_genome, True)
        args.set('out_dir', os.path.abspath(self.out_dir), True)
        args.set('work_dir', os.path.abspath(self.work_dir), True)
        args.set('augustus', self.augustus, True)
        args.set('augustus_cgp', self.augustus_cgp, True)
        args.set('augustus_pb', self.augustus_pb, True)
        args.set('augustus_species', self.augustus_species, True)
        args.set('tm_cfg', os.path.abspath(self.tm_cfg), True)
        args.set('tmr_cfg', os.path.abspath(self.tmr_cfg), True)
        args.set('augustus_cgp', self.augustus_cgp, True)
        args.set('maf_chunksize', self.maf_chunksize, True)
        args.set('maf_overlap', self.maf_overlap, True)
        args.set('pb_genome_chunksize', self.pb_genome_chunksize, True)
        args.set('pb_genome_overlap', self.pb_genome_overlap, True)
        args.set('pb_cfg', os.path.abspath(self.pb_cfg), True)
        args.set('resolve_split_genes', self.resolve_split_genes, True)
        args.set('augustus_cgp_cfg_template', os.path.abspath(self.augustus_cgp_cfg_template), True)
        args.set('cgp_param', os.path.abspath(self.cgp_param), True)
        
        # user specified flags for consensus finding
        args.set('intron_rnaseq_support', self.intron_rnaseq_support, False)
        args.set('exon_rnaseq_support', self.exon_rnaseq_support, False)
        args.set('intron_annot_support', self.intron_annot_support, False)
        args.set('exon_annot_support', self.exon_annot_support, False)
        args.set('original_intron_support', self.original_intron_support, False)
        args.set('denovo_num_introns', self.denovo_num_introns, False)
        args.set('denovo_splice_support', self.denovo_splice_support, False)
        args.set('denovo_exon_support', self.denovo_exon_support, False)
        args.set('minimum_coverage', self.minimum_coverage, False)
        args.set('require_pacbio_support', self.require_pacbio_support, False)
        args.set('in_species_rna_support_only', self.in_species_rna_support_only, False)
        args.set('rebuild_consensus', self.rebuild_consensus, False)

        # flags for assembly hub building
        args.set('assembly_hub', self.assembly_hub, False)  # assembly hub doesn't need to cause rebuild of gene sets

        args.set('hal_genomes', tuple(tools.hal.extract_genomes(self.hal)), True)
        if self.target_genomes is None:
            args.set('target_genomes', tuple(set(args.hal_genomes) - {self.ref_genome}), True)
        else:
            args.set('target_genomes', tuple([x for x in self.target_genomes]), True)

        args.set('cfg', self.parse_cfg(), True)
        args.set('modes', self.get_modes(args), True)
        args.set('augustus_tmr', True if 'augTMR' in args.modes else False, True)
        args.set('dbs', PipelineTask.get_databases(args), True)
        args.set('annotation', args.cfg['ANNOTATION'][args.ref_genome], True)
        args.set('hints_db', os.path.join(args.work_dir, 'hints_database', 'hints.db'), True)
        args.set('rnaseq_genomes', frozenset(set(args.cfg['INTRONBAM'].keys()) | set(args.cfg['BAM'].keys())), True)
        args.set('intron_only_genomes', frozenset(set(args.cfg['INTRONBAM'].keys()) - set(args.cfg['BAM'].keys())), True)
        # don't include the reference genome as a isoseq_genome; we do not run AugustusPB on it
        args.set('isoseq_genomes', frozenset(set(args.cfg['ISO_SEQ_BAM'].keys()) - {self.ref_genome}), True)
        args.set('annotation_genomes', frozenset(set(args.cfg['ANNOTATION'].keys())), True)
        self.validate_cfg(args)

        # calculate the number of cores a hgm run should use
        # this is sort of a hack, but the reality is that halLiftover uses a fraction of a CPU most of the time
        num_cpu = int(tools.mathOps.format_ratio(multiprocessing.cpu_count(), len(args.modes)))
        args.set('hgm_cpu', num_cpu, False)
        return args

    def parse_cfg(self):
        """
        Parses the input config file. Config file format:

        [ANNOTATION]
        annotation = /path/to/gff3

        [INTRONBAM]
        genome1 = /path/to/non_polyA_bam1.bam, /path/to/non_polyA_bam2.bam

        [BAM]
        genome1 = /path/to/fofn

        [ISO_SEQ_BAM]
        genome1 = /path/to/bam/or/fofn

        The annotation field must be populated for the reference genome.

        BAM annotations can be put either under INTRONBAM or BAM. Any INTRONBAM will only have intron data loaded,
        and is suitable for lower quality RNA-seq.

        """
        if not os.path.exists(self.config):
            raise MissingFileException('Config file {} not found.'.format(self.config))
        # configspec validates the input config file
        configspec = ['[ANNOTATION]', '__many__ = string',
                      '[INTRONBAM]', '__many__ = list',
                      '[BAM]', '__many__ = list',
                      '[ISO_SEQ_BAM]', '__many__ = list']
        parser = ConfigObj(self.config, configspec=configspec)

        # convert the config into a new dict, parsing the BAMs
        cfg = collections.defaultdict(dict)
        if 'ANNOTATION' not in parser:
            cfg['ANNOTATION'] = {}
        else:
            for genome, annot in parser['ANNOTATION'].iteritems():
                annot = os.path.abspath(annot)
                if not os.path.exists(annot):
                    raise MissingFileException('Missing annotation file {}.'.format(annot))
                cfg['ANNOTATION'][genome] = annot

        # if a given genome only has one BAM, it is a string. Fix this. Extract all paths from fofn files.
        for dtype in ['BAM', 'INTRONBAM', 'ISO_SEQ_BAM']:
            if dtype not in parser:  # the user does not have to specify all field types
                cfg[dtype] = {}
                continue
            for genome in parser[dtype]:
                path = parser[dtype][genome]
                if isinstance(path, str):
                    if not tools.misc.is_bam(path):
                        # this is a fofn
                        cfg[dtype][genome] = [os.path.abspath(x.rstrip()) for x in open(path)]
                    else:
                        # this is a single BAM
                        cfg[dtype][genome] = [os.path.abspath(path)]
                else:
                    cfg[dtype][genome] = [os.path.abspath(x) for x in path]
        # return a hashable version
        return frozendict((key, frozendict((ikey, tuple(ival) if isinstance(ival, list) else ival)
                                           for ikey, ival in val.iteritems())) for key, val in cfg.iteritems())

    def validate_cfg(self, args):
        """Validate the input config file."""
        if len(args.cfg['BAM']) + len(args.cfg['INTRONBAM']) + \
                len(args.cfg['ISO_SEQ_BAM']) + len(args.cfg['ANNOTATION']) == 0:
            logger.warning('No extrinsic data or annotations found in config. Will load genomes only.')
        elif len(args.cfg['BAM']) + len(args.cfg['INTRONBAM']) + len(args.cfg['ISO_SEQ_BAM']) == 0:
            logger.warning('No extrinsic data found in config. Will load genomes and annotation only.')

        for dtype in ['BAM', 'INTRONBAM', 'ISO_SEQ_BAM']:
            for genome in args.cfg[dtype]:
                for bam in args.cfg[dtype][genome]:
                    if not os.path.exists(bam):
                        raise MissingFileException('Missing BAM {}.'.format(bam))
                    if not os.path.exists(bam + '.bai'):
                        raise MissingFileException('Missing BAM index {}.'.format(bam + '.bai'))

        for genome, annot in args.cfg['ANNOTATION'].iteritems():
            if not os.path.exists(annot):
                raise MissingFileException('Missing annotation file {}.'.format(annot))

        if all(g in args.hal_genomes for g in args.target_genomes) is False:
            bad_genomes = set(args.hal_genomes) - set(args.target_genomes)
            err_msg = 'Genomes {} present in configuration and not present in HAL.'.format(','.join(bad_genomes))
            raise UserException(err_msg)

        if args.ref_genome not in args.cfg['ANNOTATION']:
            raise UserException('Reference genome {} did not have a provided annotation.'.format(self.ref_genome))

        # raise if the user if the user is providing dubious inputs
        if args.augustus_cgp and len(args.rnaseq_genomes) == 0:
            raise InvalidInputException('AugustusCGP is being ran without any RNA-seq hints!')
        if args.augustus_pb and len(args.isoseq_genomes) == 0:
            raise InvalidInputException('AugustusPB is being ran without any IsoSeq hints!')

    def get_modes(self, args):
        """returns a tuple of the execution modes being used here"""
        modes = ['transMap']
        if args.augustus_cgp is True:
            modes.append('augCGP')
        if args.augustus is True:
            modes.append('augTM')
            if len(args.cfg['BAM']) + len(args.cfg['INTRONBAM']) > 0:
                modes.append('augTMR')
        if args.augustus_pb is True:
            modes.append('augPB')
        return tuple(modes)

    def get_module_args(self, module, **args):
        """
        convenience wrapper that takes a parent module and propagates any required arguments to generate the full
        argument set.
        """
        pipeline_args = self.get_pipeline_args()
        return module.get_args(pipeline_args, **args)

    @staticmethod
    def get_databases(pipeline_args):
        """wrapper for get_database() that provides all of the databases"""
        dbs = {genome: PipelineTask.get_database(pipeline_args, genome) for genome in pipeline_args.hal_genomes}
        return frozendict(dbs)

    @staticmethod
    def get_database(pipeline_args, genome):
        """database paths must be resolved here to handle multiple programs accessing them"""
        base_out_dir = os.path.join(pipeline_args.out_dir, 'databases')
        return os.path.join(base_out_dir, '{}.db'.format(genome))

    @staticmethod
    def get_plot_dir(pipeline_args, genome):
        """plot base directories must be resolved here to handle multiple programs accessing them"""
        base_out_dir = os.path.join(pipeline_args.out_dir, 'plots')
        return os.path.join(base_out_dir, genome)

    @staticmethod
    def get_metrics_dir(pipeline_args, genome):
        """plot data directories must be resolved here to handle multiple programs accessing them"""
        base_out_dir = os.path.join(pipeline_args.work_dir, 'plot_data')
        return os.path.join(base_out_dir, genome)

    @staticmethod
    def write_metrics(metrics_dict, out_target):
        """write out a metrics dictionary to a path for later loading and plotting"""
        tools.fileOps.ensure_file_dir(out_target.path)
        with out_target.open('w') as outf:
            json.dump(metrics_dict, outf)


class PipelineWrapperTask(PipelineTask, luigi.WrapperTask):
    """add WrapperTask functionality to PipelineTask"""
    pass


class AbstractAtomicFileTask(PipelineTask):
    """
    Abstract Task for single files.
    """
    def run_cmd(self, cmd):
        """
        Run a external command that will produce the output file for this task to stdout. Capture this to the file.
        """
        # luigi localTargets guarantee atomicity if used as a context manager
        with self.output().open('w') as outf:
            tools.procOps.run_proc(cmd, stdout=outf)


class ToilTask(PipelineTask):
    """
    Task for launching toil pipelines from within luigi.
    """
    resources = {'toil': 1}  # all toil pipelines use 1 toil

    def __repr__(self):
        """override the PipelineTask repr to report the batch system being used"""
        base_repr = super(ToilTask, self).__repr__()
        return 'Toil' + base_repr + ' using batchSystem {}'.format(self.batchSystem)

    def prepare_toil_options(self, work_dir):
        """
        Prepares a Namespace object for Toil which has all defaults, overridden as specified
        Will see if the jobStore path exists, and if it does, assume that we need to add --restart
        :param work_dir: Parent directory where toil work will be done. jobStore will be placed inside. Will be used
        to fill in the workDir class variable.
        :return: Namespace
        """
        job_store = os.path.join(work_dir, 'jobStore')
        tools.fileOps.ensure_file_dir(job_store)
        toil_args = self.get_toil_defaults()
        toil_args.__dict__.update(vars(self))

        # this logic tries to determine if we should try and restart an existing jobStore
        if os.path.exists(job_store):
            try:
                root_job = open(os.path.join(job_store, 'rootJobStoreID')).next().rstrip()
                if not os.path.exists(os.path.join(job_store, 'tmp', root_job)):
                    shutil.rmtree(job_store)
                else:
                    toil_args.restart = True
            except OSError:
                toil_args.restart = True
            except IOError:
                shutil.rmtree(job_store)

        if toil_args.batchSystem == 'parasol' and toil_args.disableCaching is False:
            raise RuntimeError('Running parasol without disabled caching is a very bad idea.')
        if toil_args.batchSystem == 'parasol' and toil_args.workDir is None:
            raise RuntimeError('Running parasol without setting a shared work directory will not work. Please specify '
                               '--workDir.')

        if toil_args.workDir is not None:
            tools.fileOps.ensure_dir(toil_args.workDir)
        job_store = 'file:' + job_store
        toil_args.jobStore = job_store
        return toil_args

    def get_toil_defaults(self):
        """
        Extracts the default toil options as a dictionary, setting jobStore to None
        :return: dict
        """
        parser = Job.Runner.getDefaultArgumentParser()
        namespace = parser.parse_args([''])  # empty jobStore attribute
        namespace.jobStore = None  # jobStore attribute will be updated per-batch
        return namespace


class RebuildableTask(PipelineTask):
    def __init__(self, *args, **kwargs):
        """Allows us to force a task to be re-run. https://github.com/spotify/luigi/issues/595"""
        super(PipelineTask, self).__init__(*args, **kwargs)
        # To force execution, we just remove all outputs before `complete()` is called
        if self.rebuild_consensus is True:
            outputs = luigi.task.flatten(self.output())
            for out in outputs:
                if out.exists():
                    out.remove()


###
# pipeline tasks
###


class RunCat(PipelineWrapperTask):
    """
    Task that executes the entire pipeline.
    """
    def validate(self, pipeline_args):
        """General input validation"""
        if not os.path.exists(pipeline_args.hal):
            raise InputMissingException('HAL file not found at {}.'.format(pipeline_args.hal))
        for d in [pipeline_args.out_dir, pipeline_args.work_dir]:
            if not os.path.exists(d):
                if not tools.fileOps.dir_is_writeable(os.path.dirname(d)):
                    raise UserException('Cannot create directory {}.'.format(d))
            else:
                if not tools.fileOps.dir_is_writeable(d):
                    raise UserException('Directory {} is not writeable.'.format(d))
        if not os.path.exists(pipeline_args.annotation):
            raise InputMissingException('Annotation file {} not found.'.format(pipeline_args.annotation))
        # TODO: validate augustus species, tm/tmr/cgp/param files.
        if pipeline_args.ref_genome not in pipeline_args.hal_genomes:
            raise InvalidInputException('Reference genome {} not present in HAL.'.format(pipeline_args.ref_genome))
        missing_genomes = {g for g in pipeline_args.target_genomes if g not in pipeline_args.hal_genomes}
        if len(missing_genomes) > 0:
            missing_genomes = ','.join(missing_genomes)
            raise InvalidInputException('Target genomes {} not present in HAL.'.format(missing_genomes))
        if pipeline_args.ref_genome in pipeline_args.target_genomes:
            raise InvalidInputException('A target genome cannot be the reference genome.')

    def requires(self):
        pipeline_args = self.get_pipeline_args()
        self.validate(pipeline_args)
        yield self.clone(PrepareFiles)
        yield self.clone(BuildDb)
        yield self.clone(Chaining)
        yield self.clone(TransMap)
        yield self.clone(EvaluateTransMap)
        yield self.clone(FilterTransMap)
        if self.augustus is True:
            yield self.clone(Augustus)
        if self.augustus_cgp is True:
            yield self.clone(AugustusCgp)
        if self.augustus_pb is True:
            yield self.clone(AugustusPb)
            yield self.clone(IsoSeqIntronVectors)
        yield self.clone(Hgm)
        yield self.clone(AlignTranscripts)
        yield self.clone(EvaluateTranscripts)
        yield self.clone(Consensus)
        yield self.clone(Plots)
        if self.assembly_hub is True:
            yield self.clone(AssemblyHub)


class PrepareFiles(PipelineWrapperTask):
    """
    Wrapper for file preparation tasks GenomeFiles and ReferenceFiles
    """
    def requires(self):
        yield self.clone(GenomeFiles)
        yield self.clone(ReferenceFiles)


class GenomeFiles(PipelineWrapperTask):
    """
    WrapperTask for producing all genome files.

    GenomeFiles -> GenomeFasta -> GenomeTwoBit -> GenomeFlatFasta -> GenomeFastaIndex
                -> GenomeSizes

    """
    @staticmethod
    def get_args(pipeline_args, genome):
        base_dir = os.path.join(pipeline_args.work_dir, 'genome_files')
        args = tools.misc.HashableNamespace()
        args.genome = genome
        args.fasta = os.path.join(base_dir, genome + '.fa')
        args.two_bit = os.path.join(base_dir, genome + '.2bit')
        args.sizes = os.path.join(base_dir, genome + '.chrom.sizes')
        args.flat_fasta = os.path.join(base_dir, genome + '.fa.flat')
        return args

    def validate(self):
        for haltool in ['hal2fasta', 'halStats']:
            if not tools.misc.is_exec(haltool):
                    raise ToolMissingException('{} from the HAL tools package not in global path'.format(haltool))
        if not tools.misc.is_exec('faToTwoBit'):
            raise ToolMissingException('faToTwoBit tool from the Kent tools package not in global path.')
        if not tools.misc.is_exec('pyfasta'):
            raise ToolMissingException('pyfasta wrapper not found in global path.')

    def requires(self):
        self.validate()
        pipeline_args = self.get_pipeline_args()
        for genome in pipeline_args.hal_genomes:
            args = self.get_args(pipeline_args, genome)
            yield self.clone(GenomeFasta, **vars(args))
            yield self.clone(GenomeTwoBit, **vars(args))
            yield self.clone(GenomeSizes, **vars(args))
            yield self.clone(GenomeFlatFasta, **vars(args))


class GenomeFasta(AbstractAtomicFileTask):
    """
    Produce a fasta file from a hal file. Requires hal2fasta.
    """
    genome = luigi.Parameter()
    fasta = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.fasta)

    def run(self):
        logger.info('Extracting fasta for {}.'.format(self.genome))
        cmd = ['hal2fasta', self.hal, self.genome]
        self.run_cmd(cmd)


@requires(GenomeFasta)
class GenomeTwoBit(AbstractAtomicFileTask):
    """
    Produce a 2bit file from a fasta file. Requires kent tool faToTwoBit.
    Needs to be done BEFORE we flatten.
    """
    two_bit = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.two_bit)

    def run(self):
        logger.info('Converting fasta for {} to 2bit.'.format(self.genome))
        cmd = ['faToTwoBit', self.fasta, '/dev/stdout']
        self.run_cmd(cmd)


class GenomeSizes(AbstractAtomicFileTask):
    """
    Produces a genome chromosome sizes file. Requires halStats.
    """
    genome = luigi.Parameter()
    sizes = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.sizes)

    def run(self):
        logger.info('Extracting chromosome sizes for {}.'.format(self.genome))
        cmd = ['halStats', '--chromSizes', self.genome, self.hal]
        self.run_cmd(cmd)


@requires(GenomeTwoBit)
class GenomeFlatFasta(AbstractAtomicFileTask):
    """
    Flattens a genome fasta in-place using pyfasta. Requires the pyfasta package.
    """
    flat_fasta = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.flat_fasta)

    def run(self):
        logger.info('Flattening fasta for {}.'.format(self.genome))
        cmd = ['pyfasta', 'flatten', self.fasta]
        tools.procOps.run_proc(cmd)


class ReferenceFiles(PipelineWrapperTask):
    """
    WrapperTask for producing annotation files.

    ReferenceFiles -> Gff3ToGenePred -> TranscriptBed -> TranscriptFasta -> FlatTranscriptFasta
                            V
                         FakePsl, TranscriptGtf
    """
    @staticmethod
    def get_args(pipeline_args):
        base_dir = os.path.join(pipeline_args.work_dir, 'reference')
        annotation = os.path.splitext(os.path.basename(pipeline_args.annotation))[0]
        args = tools.misc.HashableNamespace()
        args.annotation_gp = os.path.join(base_dir, annotation + '.gp')
        args.annotation_gtf = os.path.join(base_dir, annotation + '.gtf')
        args.transcript_fasta = os.path.join(base_dir, annotation + '.fa')
        args.transcript_flat_fasta = os.path.join(base_dir, annotation + '.fa.flat')
        args.transcript_bed = os.path.join(base_dir, annotation + '.bed')
        args.ref_psl = os.path.join(base_dir, annotation + '.psl')
        args.__dict__.update(**vars(GenomeFiles.get_args(pipeline_args, pipeline_args.ref_genome)))
        return args

    def validate(self):
        for tool in ['gff3ToGenePred', 'genePredToBed', 'genePredToFakePsl']:
            if not tools.misc.is_exec(tool):
                    raise ToolMissingException('{} from the Kent tools package not in global path'.format(tool))

    def requires(self):
        self.validate()
        pipeline_args = self.get_pipeline_args()
        args = self.get_args(pipeline_args)
        yield self.clone(Gff3ToGenePred, **vars(args))
        yield self.clone(Gff3ToAttrs, **vars(args))
        yield self.clone(TranscriptBed, **vars(args))
        yield self.clone(TranscriptFasta, **vars(args))
        yield self.clone(TranscriptGtf, **vars(args))
        yield self.clone(FlatTranscriptFasta, **vars(args))
        yield self.clone(FakePsl, **vars(args))


class Gff3ToGenePred(AbstractAtomicFileTask):
    """
    Generates a genePred from a gff3 file.
    """
    annotation_gp = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.annotation_gp)

    def run(self):
        pipeline_args = self.get_pipeline_args()
        logger.info('Converting annotation gff3 to genePred.')
        cmd = ['gff3ToGenePred', '-rnaNameAttr=transcript_id', '-geneNameAttr=gene_id', '-honorStartStopCodons',
               pipeline_args.annotation, '/dev/stdout']
        self.run_cmd(cmd)


class Gff3ToAttrs(PipelineTask):
    """
    Uses the gff3 parser to extract the attributes table, converting the table into a sqlite database.
    """
    table = tools.sqlInterface.Annotation.__tablename__

    def output(self):
        pipeline_args = self.get_pipeline_args()
        database = pipeline_args.dbs[pipeline_args.ref_genome]
        tools.fileOps.ensure_file_dir(database)
        conn_str = 'sqlite:///{}'.format(database)
        digest = tools.fileOps.hashfile(pipeline_args.annotation)
        attrs_table = luigi.contrib.sqla.SQLAlchemyTarget(connection_string=conn_str,
                                                          target_table=self.table,
                                                          update_id='_'.join([self.table, digest]))
        return attrs_table

    def requires(self):
        pipeline_args = self.get_pipeline_args()
        return self.clone(Gff3ToGenePred, annotation_gp=ReferenceFiles.get_args(pipeline_args).annotation_gp)

    def validate(self, pipeline_args, results):
        """Ensure that after attribute extraction we have the same number of transcripts"""
        annotation_gp = ReferenceFiles.get_args(pipeline_args).annotation_gp
        num_gp_entries = len(open(annotation_gp).readlines())
        if len(results) != num_gp_entries:
            raise UserException('The number of transcripts parsed out of the gff3 ({}) did not match the number '
                                'present ({}). Please validate your gff3. '
                                'See the documentation for info.'.format(num_gp_entries, len(results)))

    def run(self):
        logger.info('Extracting gff3 attributes to sqlite database.')
        pipeline_args = self.get_pipeline_args()
        results = tools.gff3.extract_attrs(pipeline_args.annotation)
        self.validate(pipeline_args, results)
        if 'protein_coding' not in results.TranscriptBiotype[1] or 'protein_coding' not in results.GeneBiotype[1]:
            logger.warning('No protein_coding annotations found!')
        database = pipeline_args.dbs[pipeline_args.ref_genome]
        with tools.sqlite.ExclusiveSqlConnection(database) as engine:
            results.to_sql(self.table, engine, if_exists='replace')
        self.output().touch()


@requires(Gff3ToGenePred)
class TranscriptBed(AbstractAtomicFileTask):
    """
    Produces a BED record from the input genePred annotation. Makes use of Kent tool genePredToBed
    """
    transcript_bed = luigi.Parameter()
    annotation_gp = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.transcript_bed)

    def run(self):
        logger.info('Converting annotation genePred to BED.')
        cmd = ['genePredToBed', self.annotation_gp, '/dev/stdout']
        self.run_cmd(cmd)


@multiple_requires(GenomeFlatFasta, TranscriptBed)
class TranscriptFasta(AbstractAtomicFileTask):
    """
    Produces a fasta for each transcript.
    """
    transcript_fasta = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.transcript_fasta)

    def run(self):
        logger.info('Extracting reference annotation fasta.')
        seq_dict = tools.bio.get_sequence_dict(self.fasta, upper=False)
        seqs = {tx.name: tx.get_mrna(seq_dict) for tx in tools.transcripts.transcript_iterator(self.transcript_bed)}
        with self.output().open('w') as outf:
            for name, seq in seqs.iteritems():
                tools.bio.write_fasta(outf, name, seq)


@requires(Gff3ToGenePred)
class TranscriptGtf(AbstractAtomicFileTask):
    """
    Produces a GTF out of the genePred for the reference
    """
    annotation_gtf = luigi.Parameter()
    annotation_gp = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.annotation_gtf)

    def run(self):
        logger.info('Extracting reference annotation GTF.')
        tools.misc.convert_gp_gtf(self.output(), luigi.LocalTarget(self.annotation_gp))


@requires(TranscriptFasta)
class FlatTranscriptFasta(AbstractAtomicFileTask):
    """
    Flattens the transcript fasta for pyfasta.
    """
    transcript_fasta = luigi.Parameter()
    transcript_flat_fasta = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.transcript_flat_fasta)

    def run(self):
        logger.info('Flattening reference annotation fasta.')
        cmd = ['pyfasta', 'flatten', self.transcript_fasta]
        tools.procOps.run_proc(cmd)


@multiple_requires(Gff3ToGenePred, GenomeSizes)
class FakePsl(AbstractAtomicFileTask):
    """
    Produces a fake PSL mapping transcripts to the genome, using the Kent tool genePredToFakePsl
    """
    ref_psl = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.ref_psl)

    def run(self):
        logger.info('Generating annotation fake PSL.')
        cmd = ['genePredToFakePsl', '-chromSize={}'.format(self.sizes), 'noDB',
               self.annotation_gp, '/dev/stdout', '/dev/null']
        self.run_cmd(cmd)


class BuildDb(PipelineTask):
    """
    Constructs the hints database from a series of reduced hints GFFs.

    TODO: output() should be way smarter than this. Currently, it only checks if the indices have been created.
    """
    @staticmethod
    def get_args(pipeline_args, genome):
        base_dir = os.path.join(pipeline_args.work_dir, 'hints_database')
        args = tools.misc.HashableNamespace()
        args.genome = genome
        args.fasta = GenomeFiles.get_args(pipeline_args, genome).fasta
        args.hal = pipeline_args.hal
        args.cfg = pipeline_args.cfg
        args.annotation = pipeline_args.cfg['ANNOTATION'].get(genome, None)
        args.hints_path = os.path.join(base_dir, genome + '.extrinsic_hints.gff')
        return args

    def validate(self):
        tools.misc.samtools_version()  # validate samtools version
        for tool in ['load2sqlitedb', 'samtools', 'filterBam', 'bam2hints', 'bam2wig', 'wig2hints.pl', 'bam2hints',
                     'bamToPsl', 'blat2hints.pl', 'gff3ToGenePred', 'join_mult_hints.pl']:
            if not tools.misc.is_exec(tool):
                raise ToolMissingException('Auxiliary program {} not found on path.'.format(tool))

    def requires(self):
        pipeline_args = self.get_pipeline_args()
        for genome in pipeline_args.hal_genomes:
            hints_args = BuildDb.get_args(pipeline_args, genome)
            yield self.clone(GenerateHints, hints_args=hints_args, genome=genome)

    def output(self):
        pipeline_args = self.get_pipeline_args()
        tools.fileOps.ensure_file_dir(pipeline_args.hints_db)
        return IndexTarget(pipeline_args.hints_db)

    def run(self):
        pipeline_args = self.get_pipeline_args()
        self.validate()
        for genome in pipeline_args.hal_genomes:
            args = BuildDb.get_args(pipeline_args, genome)
            logger.info('Loading sequence for {} into database.'.format(genome))
            base_cmd = ['load2sqlitedb', '--noIdx', '--clean', '--species={}'.format(genome),
                        '--dbaccess={}'.format(pipeline_args.hints_db)]
            tools.procOps.run_proc(base_cmd + [args.fasta])
            if os.path.getsize(args.hints_path) != 0:
                logger.info('Loading hints for {} into database.'.format(genome))
                tools.procOps.run_proc(base_cmd + [args.hints_path])
        logger.info('Indexing database.')
        cmd = ['load2sqlitedb', '--makeIdx', '--clean', '--dbaccess={}'.format(pipeline_args.hints_db)]
        tools.procOps.run_proc(cmd)
        logger.info('Hints database completed.')


class GenerateHints(ToilTask):
    """
    Generate hints for each genome as a separate Toil pipeline.
    """
    hints_args = luigi.Parameter()
    genome = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.hints_args.hints_path)

    def requires(self):
        return self.clone(PrepareFiles), self.clone(ReferenceFiles)

    def run(self):
        logger.info('Beginning GenerateHints Toil pipeline for {}.'.format(self.genome))
        work_dir = os.path.abspath(os.path.join(self.work_dir, 'toil', 'hints_db', self.genome))
        toil_options = self.prepare_toil_options(work_dir)
        hints_db(self.hints_args, toil_options)
        logger.info('Finished GenerateHints Toil pipeline for {}.'.format(self.genome))


class Chaining(ToilTask):
    """
    Task that launches the Chaining toil pipeline. This pipeline operates on all genomes at once to reduce the
    repeated downloading of the HAL file.
    """
    @staticmethod
    def get_args(pipeline_args):
        base_dir = os.path.join(pipeline_args.work_dir, 'chaining')
        ref_files = GenomeFiles.get_args(pipeline_args, pipeline_args.ref_genome)
        tgt_files = {genome: GenomeFiles.get_args(pipeline_args, genome) for genome in pipeline_args.target_genomes}
        tgt_two_bits = {genome: tgt_files[genome].two_bit for genome in pipeline_args.target_genomes}
        chain_files = {genome: os.path.join(base_dir, '{}-{}.chain'.format(pipeline_args.ref_genome, genome))
                       for genome in pipeline_args.target_genomes}
        args = tools.misc.HashableNamespace()
        args.hal = pipeline_args.hal
        args.ref_genome = pipeline_args.ref_genome
        args.query_two_bit = ref_files.two_bit
        args.query_sizes = ref_files.sizes
        args.target_two_bits = tgt_two_bits
        args.chain_files = chain_files
        return args

    def output(self):
        pipeline_args = self.get_pipeline_args()
        chain_args = self.get_args(pipeline_args)
        for path in chain_args.chain_files.itervalues():
            yield luigi.LocalTarget(path)

    def validate(self):
        if not tools.misc.is_exec('halLiftover'):
            raise ToolMissingException('halLiftover from the halTools package not in global path.')
        for tool in ['pslPosTarget', 'axtChain', 'chainMergeSort']:
            if not tools.misc.is_exec(tool):
                    raise ToolMissingException('{} from the Kent tools package not in global path.'.format(tool))

    def requires(self):
        yield self.clone(PrepareFiles)

    def run(self):
        self.validate()
        pipeline_args = self.get_pipeline_args()
        logger.info('Launching Pairwise Chaining toil pipeline.')
        toil_work_dir = os.path.join(self.work_dir, 'toil', 'chaining')
        toil_options = self.prepare_toil_options(toil_work_dir)
        chain_args = self.get_args(pipeline_args)
        chaining(chain_args, toil_options)
        logger.info('Pairwise Chaining toil pipeline is complete.')


class TransMap(PipelineWrapperTask):
    """
    Runs transMap.
    """
    @staticmethod
    def get_args(pipeline_args, genome):
        base_dir = os.path.join(pipeline_args.work_dir, 'transMap')
        ref_files = ReferenceFiles.get_args(pipeline_args)
        args = tools.misc.HashableNamespace()
        args.two_bit = GenomeFiles.get_args(pipeline_args, genome).two_bit
        args.chain_file = Chaining.get_args(pipeline_args).chain_files[genome]
        args.transcript_fasta = ref_files.transcript_fasta
        args.ref_psl = ref_files.ref_psl
        args.annotation_gp = ref_files.annotation_gp
        args.tm_psl = os.path.join(base_dir, genome + '.psl')
        args.tm_gp = os.path.join(base_dir, genome + '.gp')
        args.tm_gtf = os.path.join(base_dir, genome + '.gtf')
        return args

    def validate(self):
        for tool in ['pslMap', 'pslRecalcMatch', 'pslMapPostChain', 'pslCDnaFilter']:
            if not tools.misc.is_exec(tool):
                    raise ToolMissingException('{} from the Kent tools package not in global path.'.format(tool))

    def requires(self):
        self.validate()
        pipeline_args = self.get_pipeline_args()
        for target_genome in pipeline_args.target_genomes:
            yield self.clone(TransMapPsl, genome=target_genome)
            yield self.clone(TransMapGp, genome=target_genome)
            yield self.clone(TransMapGtf, genome=target_genome)


class TransMapPsl(PipelineTask):
    """
    Runs transMap. Requires Kent tools pslMap, postTransMapChain, pslRecalcMatch
    """
    genome = luigi.Parameter()

    def output(self):
        tm_args = self.get_module_args(TransMap, genome=self.genome)
        return luigi.LocalTarget(tm_args.tm_psl)

    def requires(self):
        return self.clone(PrepareFiles), self.clone(Chaining)

    def run(self):
        logger.info('Running transMap for {}.'.format(self.genome))
        tm_args = self.get_module_args(TransMap, genome=self.genome)
        cmd = [['pslMap', '-chainMapFile', tm_args.ref_psl, tm_args.chain_file, '/dev/stdout'],
               ['pslMapPostChain', '/dev/stdin', '/dev/stdout'],
               ['sort', '-k', '14,14', '-k', '16,16n'],
               ['pslRecalcMatch', '/dev/stdin', tm_args.two_bit, tm_args.transcript_fasta, 'stdout'],
               ['pslCDnaFilter', '-localNearBest=0.0001', '-minCover=0.1', '/dev/stdin', '/dev/stdout'],
               ['awk', '$17 - $16 < 3000000 {print $0}']]  # hard coded filter for 3mb transcripts
        # hacky way to make unique - capture output to a file, then process
        tmp_file = luigi.LocalTarget(is_tmp=True)
        with tmp_file.open('w') as tmp_fh:
            tools.procOps.run_proc(cmd, stdout=tmp_fh, stderr='/dev/null')
        tools.fileOps.ensure_file_dir(self.output().path)
        with self.output().open('w') as outf:
            for psl_rec in tools.psl.psl_iterator(tmp_file.path, make_unique=True):
                outf.write('\t'.join(psl_rec.psl_string()) + '\n')


@requires(TransMapPsl)
class TransMapGp(AbstractAtomicFileTask):
    """
    Produces the final transMapped genePred
    """
    def output(self):
        tm_args = self.get_module_args(TransMap, genome=self.genome)
        return luigi.LocalTarget(tm_args.tm_gp)

    def run(self):
        tm_args = self.get_module_args(TransMap, genome=self.genome)
        logger.info('Converting transMap PSL to genePred for {}.'.format(self.genome))
        cmd = ['transMapPslToGenePred', '-nonCodingGapFillMax=80', '-codingGapFillMax=50',
               tm_args.annotation_gp, tm_args.tm_psl, '/dev/stdout']
        self.run_cmd(cmd)


@requires(TransMapGp)
class TransMapGtf(PipelineTask):
    """
    Converts the transMap genePred to GTF
    """
    def output(self):
        tm_args = self.get_module_args(TransMap, genome=self.genome)
        return luigi.LocalTarget(tm_args.tm_gtf)

    def run(self):
        tm_args = self.get_module_args(TransMap, genome=self.genome)
        logger.info('Converting transMap genePred to GTF for {}.'.format(self.genome))
        tools.misc.convert_gp_gtf(self.output(), luigi.LocalTarget(tm_args.tm_gp))


class EvaluateTransMap(PipelineWrapperTask):
    """
    Evaluates transMap alignments.
    """
    @staticmethod
    def get_args(pipeline_args, genome):
        tm_args = TransMap.get_args(pipeline_args, genome)
        args = tools.misc.HashableNamespace()
        args.db_path = pipeline_args.dbs[genome]
        args.tm_psl = tm_args.tm_psl
        args.ref_psl = ReferenceFiles.get_args(pipeline_args).ref_psl
        args.tm_gp = tm_args.tm_gp
        args.annotation_gp = tm_args.annotation_gp
        args.annotation_gp = ReferenceFiles.get_args(pipeline_args).annotation_gp
        args.genome = genome
        args.fasta = GenomeFiles.get_args(pipeline_args, genome).fasta
        args.ref_genome = pipeline_args.ref_genome
        return args

    def validate(self):
        pass

    def requires(self):
        self.validate()
        pipeline_args = self.get_pipeline_args()
        for target_genome in pipeline_args.target_genomes:
            tm_eval_args = EvaluateTransMap.get_args(pipeline_args, target_genome)
            yield self.clone(EvaluateTransMapDriverTask, tm_eval_args=tm_eval_args, genome=target_genome)


class EvaluateTransMapDriverTask(PipelineTask):
    """
    Task for per-genome launching of a toil pipeline for aligning transcripts to their parent.
    """
    genome = luigi.Parameter()
    tm_eval_args = luigi.Parameter()
    table = tools.sqlInterface.TmEval.__tablename__

    def write_to_sql(self, df):
        """Load the results into the SQLite database"""
        with tools.sqlite.ExclusiveSqlConnection(self.tm_eval_args.db_path) as engine:
            df.to_sql(self.table, engine, if_exists='replace')
            self.output().touch()
            logger.info('Loaded table: {}.{}'.format(self.genome, self.table))

    def output(self):
        pipeline_args = self.get_pipeline_args()
        tools.fileOps.ensure_file_dir(self.tm_eval_args.db_path)
        conn_str = 'sqlite:///{}'.format(self.tm_eval_args.db_path)
        return luigi.contrib.sqla.SQLAlchemyTarget(connection_string=conn_str,
                                                   target_table=self.table,
                                                   update_id='_'.join([self.table, str(hash(pipeline_args))]))

    def requires(self):
        return self.clone(TransMap), self.clone(ReferenceFiles)

    def run(self):
        logger.info('Evaluating transMap results for {}.'.format(self.genome))
        results = transmap_classify(self.tm_eval_args)
        self.write_to_sql(results)


class FilterTransMap(PipelineWrapperTask):
    """
    Filters transMap alignments for paralogs, as well as multiple chromosomes if the --resolve-split-genes flag is set.
    """
    @staticmethod
    def get_args(pipeline_args, genome):
        base_dir = os.path.join(pipeline_args.work_dir, 'filtered_transMap')
        args = tools.misc.HashableNamespace()
        args.genome = genome
        args.tm_gp = TransMap.get_args(pipeline_args, genome).tm_gp
        args.filtered_tm_gp = os.path.join(base_dir, genome + '.filtered.gp')
        args.filtered_tm_gtf = os.path.join(base_dir, genome + '.filtered.gtf')
        args.db_path = pipeline_args.dbs[genome]
        args.ref_db_path = pipeline_args.dbs[pipeline_args.ref_genome]
        args.resolve_split_genes = pipeline_args.resolve_split_genes
        args.metrics_json = os.path.join(PipelineTask.get_metrics_dir(pipeline_args, genome), 'filter_tm_metrics.json')
        return args

    def validate(self):
        pass

    def requires(self):
        self.validate()
        pipeline_args = self.get_pipeline_args()
        for target_genome in pipeline_args.target_genomes:
            filter_tm_args = FilterTransMap.get_args(pipeline_args, target_genome)
            yield self.clone(FilterTransMapDriverTask, filter_tm_args=filter_tm_args, genome=target_genome)


class FilterTransMapDriverTask(PipelineTask):
    """
    Driver task for per-genome transMap filtering.
    """
    genome = luigi.Parameter()
    filter_tm_args = luigi.Parameter()
    eval_table = tools.sqlInterface.TmFilterEval.__tablename__
    cutoff_table = tools.sqlInterface.TmFit.__tablename__

    def write_to_sql(self, updated_df, fit_df, filter_table_target, fit_table_target):
        """Load the results into the SQLite database"""
        with tools.sqlite.ExclusiveSqlConnection(self.filter_tm_args.db_path) as engine:
            updated_df.to_sql(self.eval_table, engine, if_exists='replace')
            filter_table_target.touch()
            logger.info('Loaded table: {}.{}'.format(self.genome, self.eval_table))
            fit_df.to_sql(self.cutoff_table, engine, if_exists='replace')
            fit_table_target.touch()
            logger.info('Loaded table: {}.{}'.format(self.genome, self.cutoff_table))

    def output(self):
        pipeline_args = self.get_pipeline_args()
        tools.fileOps.ensure_file_dir(self.filter_tm_args.db_path)
        conn_str = 'sqlite:///{}'.format(self.filter_tm_args.db_path)
        return (luigi.contrib.sqla.SQLAlchemyTarget(connection_string=conn_str,
                                                    target_table=self.eval_table,
                                                    update_id='_'.join([self.eval_table, str(hash(pipeline_args))])),
                luigi.contrib.sqla.SQLAlchemyTarget(connection_string=conn_str,
                                                    target_table=self.cutoff_table,
                                                    update_id='_'.join([self.cutoff_table, str(hash(pipeline_args))])),
                luigi.LocalTarget(self.filter_tm_args.filtered_tm_gp),
                luigi.LocalTarget(self.filter_tm_args.filtered_tm_gtf),
                luigi.LocalTarget(self.filter_tm_args.metrics_json))

    def requires(self):
        return self.clone(EvaluateTransMap)

    def run(self):
        logger.info('Filtering transMap results for {}.'.format(self.genome))
        filter_table_target, fit_table_target, filtered_tm_gp, filtered_tm_gtf, metrics_json = self.output()
        metrics_dict, updated_df, fit_df = filter_transmap(self.filter_tm_args, filtered_tm_gp)
        PipelineTask.write_metrics(metrics_dict, metrics_json)
        tools.misc.convert_gp_gtf(filtered_tm_gtf, filtered_tm_gp)
        self.write_to_sql(updated_df, fit_df, filter_table_target, fit_table_target)


class Augustus(PipelineWrapperTask):
    """
    Runs AugustusTM(R) on the coding output from transMap.
    """
    @staticmethod
    def get_args(pipeline_args, genome):
        base_dir = os.path.join(pipeline_args.work_dir, 'augustus')
        args = tools.misc.HashableNamespace()
        args.ref_genome = pipeline_args.ref_genome
        args.genome = genome
        args.genome_fasta = GenomeFiles.get_args(pipeline_args, genome).fasta
        args.annotation_gp = ReferenceFiles.get_args(pipeline_args).annotation_gp
        args.ref_db_path = PipelineTask.get_database(pipeline_args, pipeline_args.ref_genome)
        args.filtered_tm_gp = FilterTransMap.get_args(pipeline_args, genome).filtered_tm_gp
        tm_args = TransMap.get_args(pipeline_args, genome)
        args.ref_psl = tm_args.ref_psl
        args.tm_psl = tm_args.tm_psl
        args.augustus_tm_gp = os.path.join(base_dir, genome + '.augTM.gp')
        args.augustus_tm_gtf = os.path.join(base_dir, genome + '.augTM.gtf')
        args.tm_cfg = pipeline_args.tm_cfg
        args.tmr_cfg = pipeline_args.tmr_cfg
        args.augustus_species = pipeline_args.augustus_species
        args.augustus_hints_db = pipeline_args.hints_db
        args.augustus_tmr = genome in pipeline_args.rnaseq_genomes
        if args.augustus_tmr:
            args.augustus_tmr_gp = os.path.join(base_dir, genome + '.augTMR.gp')
            args.augustus_tmr_gtf = os.path.join(base_dir, genome + '.augTMR.gtf')
        return args

    def validate(self):
        for tool in ['augustus', 'transMap2hints.pl']:
            if not tools.misc.is_exec(tool):
                raise ToolMissingException('Auxiliary program {} from the Augustus package not in path.'.format(tool))

    def requires(self):
        self.validate()
        pipeline_args = self.get_pipeline_args()
        for target_genome in pipeline_args.target_genomes:
            yield self.clone(AugustusDriverTask, genome=target_genome)


class AugustusDriverTask(ToilTask):
    """
    Task for per-genome launching of a toil pipeline for running Augustus.
    """
    genome = luigi.Parameter()

    def output(self):
        pipeline_args = self.get_pipeline_args()
        augustus_args = Augustus.get_args(pipeline_args, self.genome)
        yield luigi.LocalTarget(augustus_args.augustus_tm_gp)
        yield luigi.LocalTarget(augustus_args.augustus_tm_gtf)
        if augustus_args.augustus_tmr:
            yield luigi.LocalTarget(augustus_args.augustus_tmr_gp)
            yield luigi.LocalTarget(augustus_args.augustus_tmr_gtf)

    def requires(self):
        return self.clone(FilterTransMap), self.clone(BuildDb)

    def extract_coding_genes(self, augustus_args):
        """extracts only coding genes from the input genePred, returning a path to a tmp file"""
        coding_gp = tools.fileOps.get_tmp_file()
        attrs = tools.sqlInterface.read_attrs(augustus_args.ref_db_path)
        names = set(attrs[attrs.TranscriptBiotype == 'protein_coding'].index)
        with open(coding_gp, 'w') as outf:
            for tx in tools.transcripts.gene_pred_iterator(augustus_args.filtered_tm_gp):
                if tools.nameConversions.strip_alignment_numbers(tx.name) in names:
                    tools.fileOps.print_row(outf, tx.get_gene_pred())
        if os.path.getsize(coding_gp) == 0:
            raise InvalidInputException('Unable to extract coding transcripts from the filtered transMap genePred.')
        return coding_gp

    def run(self):
        toil_work_dir = os.path.join(self.work_dir, 'toil', 'augustus', self.genome)
        logger.info('Launching AugustusTMR toil pipeline on {}.'.format(self.genome))
        toil_options = self.prepare_toil_options(toil_work_dir)
        augustus_args = self.get_module_args(Augustus, genome=self.genome)
        coding_gp = self.extract_coding_genes(augustus_args)
        augustus(augustus_args, coding_gp, toil_options)
        logger.info('Augustus toil pipeline for {} completed.'.format(self.genome))
        os.remove(coding_gp)
        for out_gp, out_gtf in tools.misc.pairwise(self.output()):
            tools.misc.convert_gtf_gp(out_gp, out_gtf)


class AugustusCgp(ToilTask):
    """
    Task for launching the AugustusCGP toil pipeline
    """
    tablename = tools.sqlInterface.AugCgpAlternativeGenes.__tablename__

    @staticmethod
    def get_args(pipeline_args):
        # add reference to the target genomes
        tgt_genomes = list(pipeline_args.target_genomes) + [pipeline_args.ref_genome]
        fasta_files = {genome: GenomeFiles.get_args(pipeline_args, genome).fasta for genome in tgt_genomes}
        base_dir = os.path.join(pipeline_args.work_dir, 'augustus_cgp')
        # output
        output_gp_files = {genome: os.path.join(base_dir, genome + '.augCGP.gp') for genome in tgt_genomes}
        output_gtf_files = {genome: os.path.join(base_dir, genome + '.augCGP.gtf') for genome in tgt_genomes}
        raw_output_gtf_files = {genome: os.path.join(base_dir, genome + '.raw.augCGP.gtf') for genome in tgt_genomes}
        # transMap files used for assigning parental gene
        filtered_tm_gp_files = {genome: FilterTransMap.get_args(pipeline_args, genome).filtered_tm_gp
                                for genome in pipeline_args.target_genomes}
        unfiltered_tm_gp_files = {genome: TransMap.get_args(pipeline_args, genome).tm_gp
                                  for genome in pipeline_args.target_genomes}
        # add the reference annotation as a pseudo-transMap to assign parents in reference
        filtered_tm_gp_files[pipeline_args.ref_genome] = ReferenceFiles.get_args(pipeline_args).annotation_gp
        unfiltered_tm_gp_files[pipeline_args.ref_genome] = ReferenceFiles.get_args(pipeline_args).annotation_gp
        args = tools.misc.HashableNamespace()
        args.genomes = tgt_genomes
        args.fasta_files = fasta_files
        args.filtered_tm_gps = filtered_tm_gp_files
        args.unfiltered_tm_gps = unfiltered_tm_gp_files
        args.hal = pipeline_args.hal
        args.ref_genome = pipeline_args.ref_genome
        args.augustus_cgp_gp = output_gp_files
        args.augustus_cgp_gtf = output_gtf_files
        args.augustus_cgp_raw_gtf = raw_output_gtf_files
        args.species = pipeline_args.augustus_species
        args.chunksize = pipeline_args.maf_chunksize
        args.overlap = pipeline_args.maf_overlap
        args.cgp_param = pipeline_args.cgp_param
        args.hints_db = pipeline_args.hints_db
        args.ref_db_path = PipelineTask.get_database(pipeline_args, pipeline_args.ref_genome)
        args.query_sizes = GenomeFiles.get_args(pipeline_args, pipeline_args.ref_genome).sizes
        return args

    def output(self):
        pipeline_args = self.get_pipeline_args()
        cgp_args = self.get_args(pipeline_args)
        for path_dict in [cgp_args.augustus_cgp_gp, cgp_args.augustus_cgp_gtf, cgp_args.augustus_cgp_raw_gtf]:
            for path in path_dict.itervalues():
                yield luigi.LocalTarget(path)
        for genome in itertools.chain(pipeline_args.target_genomes, [pipeline_args.ref_genome]):
            yield self.get_table_targets(genome, pipeline_args)

    def get_table_targets(self, genome, pipeline_args):
        db = pipeline_args.dbs[genome]
        tools.fileOps.ensure_file_dir(db)
        conn_str = 'sqlite:///{}'.format(db)
        return luigi.contrib.sqla.SQLAlchemyTarget(connection_string=conn_str,
                                                   target_table=self.tablename,
                                                   update_id='_'.join([self.tablename, str(hash(pipeline_args))]))

    def validate(self):
        for tool in ['joingenes', 'augustus', 'hal2maf', 'gtfToGenePred', 'genePredToGtf', 'bedtools']:
            if not tools.misc.is_exec(tool):
                raise ToolMissingException('tool {} not in global path.'.format(tool))

    def requires(self):
        yield self.clone(FilterTransMap), self.clone(Gff3ToAttrs), self.clone(BuildDb)

    def prepare_cgp_cfg(self, pipeline_args):
        """use the config template to create a config file"""
        # bam genomes have IsoSeq and/or at least one BAM
        bam_genomes = (pipeline_args.rnaseq_genomes | pipeline_args.isoseq_genomes) - \
                      (pipeline_args.annotation_genomes | pipeline_args.intron_only_genomes)
        # intron only genomes have only intron hints
        intron_only_genomes = pipeline_args.intron_only_genomes - (bam_genomes | pipeline_args.annotation_genomes)
        if not tools.mathOps.all_disjoint([bam_genomes, intron_only_genomes, pipeline_args.annotation_genomes]):
            raise UserException('Error in CGP configuration. Not all genome groups are disjoint.')
        # if --target-genomes is set, remove these genomes from the groups
        target_genomes = set(pipeline_args.target_genomes)
        target_genomes.add(pipeline_args.ref_genome)
        annotation_genomes = pipeline_args.annotation_genomes & target_genomes
        bam_genomes = bam_genomes & target_genomes
        intron_only_genomes = intron_only_genomes & target_genomes
        annotation_genomes = 'none' if len(pipeline_args.annotation_genomes) == 0 else ' '.join(annotation_genomes)
        bam_genomes = 'none' if len(bam_genomes) == 0 else ' '.join(bam_genomes)
        intron_only_genomes = 'none' if len(intron_only_genomes) == 0 else ' '.join(intron_only_genomes)
        template = open(pipeline_args.augustus_cgp_cfg_template).read()
        cfg = template.format(annotation_genomes=annotation_genomes, target_genomes=bam_genomes,
                              intron_target_genomes=intron_only_genomes)
        out_path = tools.fileOps.get_tmp_file()
        with open(out_path, 'w') as outf:
            outf.write(cfg)
        return out_path

    def load_alternative_tx_tables(self, pipeline_args, database_dfs):
        """loads the alternative transcript database for each genome"""
        for genome, df in database_dfs:
            sqla_target = self.get_table_targets(genome, pipeline_args)
            db = pipeline_args.dbs[genome]
            with tools.sqlite.ExclusiveSqlConnection(db) as engine:
                df.to_sql(self.tablename, engine, if_exists='replace')
            sqla_target.touch()
            logger.info('Loaded table: {}.{}'.format(genome, self.tablename))

    def run(self):
        self.validate()
        pipeline_args = self.get_pipeline_args()
        logger.info('Launching AugustusCGP toil pipeline.')
        toil_work_dir = os.path.join(self.work_dir, 'toil', 'augustus_cgp')
        toil_options = self.prepare_toil_options(toil_work_dir)
        cgp_args = self.get_args(pipeline_args)
        cgp_args.cgp_cfg = self.prepare_cgp_cfg(pipeline_args)
        database_dfs, fail_counts = augustus_cgp(cgp_args, toil_options)
        log_msg = 'AugustusCGP toil pipeline completed. Due to overlapping multiple transMap genes, the following ' \
                  'number of predictions were discarded: '
        log_msg += ', '.join(['{}: {}'.format(genome, count) for genome, count in fail_counts])
        logger.info(log_msg)
        # convert each to genePred as well
        for genome in itertools.chain(pipeline_args.target_genomes, [pipeline_args.ref_genome]):
            gp_target = luigi.LocalTarget(cgp_args.augustus_cgp_gp[genome])
            gtf_target = luigi.LocalTarget(cgp_args.augustus_cgp_gtf[genome])
            tools.misc.convert_gtf_gp(gp_target, gtf_target)
        logger.info('Finished converting AugustusCGP output.')
        self.load_alternative_tx_tables(pipeline_args, database_dfs)


class AugustusPb(PipelineWrapperTask):
    """
    Runs AugustusPB. This mode is done on a per-genome basis, but ignores transMap information and and relies only on
    a combination of IsoSeq and RNA-seq
    """
    @staticmethod
    def get_args(pipeline_args, genome):
        base_dir = os.path.join(pipeline_args.work_dir, 'augustus_pb')
        args = tools.misc.HashableNamespace()
        args.genome = genome
        args.genome_fasta = GenomeFiles.get_args(pipeline_args, genome).fasta
        args.annotation_gp = ReferenceFiles.get_args(pipeline_args).annotation_gp
        args.unfiltered_tm_gp = TransMap.get_args(pipeline_args, genome).tm_gp
        args.filtered_tm_gp = FilterTransMap.get_args(pipeline_args, genome).filtered_tm_gp
        args.ref_db_path = PipelineTask.get_database(pipeline_args, pipeline_args.ref_genome)
        args.pb_cfg = pipeline_args.pb_cfg
        args.chunksize = pipeline_args.pb_genome_chunksize
        args.overlap = pipeline_args.pb_genome_overlap
        args.species = pipeline_args.augustus_species
        args.hints_gff = BuildDb.get_args(pipeline_args, genome).hints_path
        args.augustus_pb_gtf = os.path.join(base_dir, genome + '.augPB.gtf')
        args.augustus_pb_gp = os.path.join(base_dir, genome + '.augPB.gp')
        args.augustus_pb_raw_gtf = os.path.join(base_dir, genome + '.raw.augPB.gtf')
        return args

    def validate(self):
        for tool in ['augustus', 'joingenes']:
            if not tools.misc.is_exec(tool):
                raise ToolMissingException('Auxiliary program {} from the Augustus package not in path.'.format(tool))

    def requires(self):
        self.validate()
        pipeline_args = self.get_pipeline_args()
        for target_genome in pipeline_args.isoseq_genomes:
            yield self.clone(AugustusPbDriverTask, genome=target_genome)


class AugustusPbDriverTask(ToilTask):
    """
    Task for per-genome launching of a toil pipeline for running AugustusPB.
    """
    genome = luigi.Parameter()
    tablename = tools.sqlInterface.AugPbAlternativeGenes.__tablename__

    def output(self):
        pipeline_args = self.get_pipeline_args()
        augustus_pb_args = AugustusPb.get_args(pipeline_args, self.genome)
        yield luigi.LocalTarget(augustus_pb_args.augustus_pb_gp)
        yield luigi.LocalTarget(augustus_pb_args.augustus_pb_gtf)
        yield luigi.LocalTarget(augustus_pb_args.augustus_pb_raw_gtf)
        db = pipeline_args.dbs[self.genome]
        tools.fileOps.ensure_file_dir(db)
        conn_str = 'sqlite:///{}'.format(db)
        yield luigi.contrib.sqla.SQLAlchemyTarget(connection_string=conn_str,
                                                  target_table=self.tablename,
                                                  update_id='_'.join([self.tablename, str(hash(pipeline_args))]))

    def requires(self):
        return self.clone(FilterTransMap), self.clone(BuildDb)

    def load_alternative_tx_tables(self, pipeline_args, df, sqla_target):
        """loads the alternative transcript database"""
        db = pipeline_args.dbs[self.genome]
        with tools.sqlite.ExclusiveSqlConnection(db) as engine:
            df.to_sql(self.tablename, engine, if_exists='replace')
        sqla_target.touch()
        logger.info('Loaded table: {}.{}'.format(self.genome, self.tablename))

    def run(self):
        pipeline_args = self.get_pipeline_args()
        toil_work_dir = os.path.join(self.work_dir, 'toil', 'augustus_pb', self.genome)
        logger.info('Launching AugustusPB toil pipeline on {}.'.format(self.genome))
        toil_options = self.prepare_toil_options(toil_work_dir)
        augustus_args = self.get_module_args(AugustusPb, genome=self.genome)
        df, fail_count = augustus_pb(augustus_args, toil_options)
        logger.info('AugustusPB toil pipeline for {} completed. {} transcript predictions were discarded due to '
                    'overlapping multiple transMap genes.'.format(self.genome, fail_count))
        out_gp, out_gtf, out_raw_gtf, sqla_target = list(self.output())
        tools.misc.convert_gtf_gp(out_gp, out_gtf)
        logger.info('Finished converting AugustusPB output.')
        self.load_alternative_tx_tables(pipeline_args, df, sqla_target)


class Hgm(PipelineWrapperTask):
    """
    Task for launching the HomGeneMapping toil pipeline. This pipeline finds the cross species RNA-seq and annotation
    support across all species.
    It will be launched once for each of transMap, AugustusTM, AugustusTMR, AugustusCGP
    """
    @staticmethod
    def get_args(pipeline_args, mode):
        base_dir = os.path.join(pipeline_args.work_dir, 'hgm', mode)
        if mode == 'augCGP':
            # add reference to the target genomes
            tgt_genomes = list(pipeline_args.target_genomes) + [pipeline_args.ref_genome]
            gtf_in_files = {genome: AugustusCgp.get_args(pipeline_args).augustus_cgp_gtf[genome]
                            for genome in tgt_genomes}
        elif mode == 'augTM':
            tgt_genomes = pipeline_args.target_genomes
            gtf_in_files = {genome: Augustus.get_args(pipeline_args, genome).augustus_tm_gtf
                            for genome in tgt_genomes}
        elif mode == 'augTMR':
            # remove reference it may have RNA-seq
            tgt_genomes = (pipeline_args.rnaseq_genomes & set(pipeline_args.target_genomes)) - {pipeline_args.ref_genome}
            gtf_in_files = {genome: Augustus.get_args(pipeline_args, genome).augustus_tmr_gtf
                            for genome in tgt_genomes}
        elif mode == 'augPB':
            tgt_genomes = set(pipeline_args.target_genomes) & pipeline_args.isoseq_genomes
            gtf_in_files = {genome: AugustusPb.get_args(pipeline_args, genome).augustus_pb_gtf
                            for genome in tgt_genomes}
        elif mode == 'transMap':
            tgt_genomes = pipeline_args.target_genomes
            gtf_in_files = {genome: FilterTransMap.get_args(pipeline_args, genome).filtered_tm_gtf
                            for genome in tgt_genomes}
        else:
            raise UserException('Invalid mode was passed to Hgm module: {}.'.format(mode))
        args = tools.misc.HashableNamespace()
        args.genomes = tgt_genomes
        args.ref_genome = pipeline_args.ref_genome
        args.hal = pipeline_args.hal
        args.in_gtf = gtf_in_files
        args.gtf_out_dir = base_dir
        args.gtf_out_files = {genome: os.path.join(base_dir, genome + '.gtf') for genome in tgt_genomes}
        args.hints_db = pipeline_args.hints_db
        args.annotation_gtf = ReferenceFiles.get_args(pipeline_args).annotation_gtf
        args.annotation_gp = ReferenceFiles.get_args(pipeline_args).annotation_gp
        args.hgm_cpu = pipeline_args.hgm_cpu
        return args

    def validate(self):
        for tool in ['homGeneMapping', 'join_mult_hints.pl']:
            if not tools.misc.is_exec(tool):
                raise ToolMissingException('auxiliary program {} from the Augustus '
                                           'package not in global path.'.format(tool))
        if not tools.misc.is_exec('halLiftover'):
            raise ToolMissingException('halLiftover from the halTools package not in global path.')
        if not tools.misc.is_exec('bedtools'):
            raise ToolMissingException('bedtools is required for the homGeneMapping module.')

    def requires(self):
        pipeline_args = self.get_pipeline_args()
        self.validate()
        for mode in pipeline_args.modes:
            yield self.clone(HgmDriverTask, mode=mode)


class HgmDriverTask(PipelineTask):
    """
    Task for running each individual instance of the Hgm pipeline. Dumps the results into a sqlite database
    Also produces a GTF file that is parsed into this database.
    """
    mode = luigi.Parameter()

    def output(self):
        pipeline_args = self.get_pipeline_args()
        hgm_args = Hgm.get_args(pipeline_args, self.mode)
        for genome in hgm_args.genomes:
            db = pipeline_args.dbs[genome]
            tools.fileOps.ensure_file_dir(db)
            conn_str = 'sqlite:///{}'.format(db)
            tablename = tools.sqlInterface.tables['hgm'][self.mode].__tablename__
            yield luigi.contrib.sqla.SQLAlchemyTarget(connection_string=conn_str,
                                                      target_table=tablename,
                                                      update_id='_'.join([tablename, str(hash(pipeline_args))]))
        for f in hgm_args.gtf_out_files.itervalues():
            yield luigi.LocalTarget(f)

    def requires(self):
        if self.mode == 'augCGP':
            yield self.clone(AugustusCgp)
        elif self.mode == 'augTM' or self.mode == 'augTMR':
            yield self.clone(Augustus)
        elif self.mode == 'transMap':
            yield self.clone(FilterTransMap)
        elif self.mode == 'augPB':
            yield self.clone(AugustusPb)
        else:
            raise UserException('Invalid mode passed to HgmDriverTask: {}.'.format(self.mode))
        yield self.clone(BuildDb)

    def run(self):
        logger.info('Launching homGeneMapping for {}.'.format(self.mode))
        pipeline_args = self.get_pipeline_args()
        hgm_args = Hgm.get_args(pipeline_args, self.mode)
        hgm(hgm_args)
        # convert the output to a dataframe and write to the genome database
        databases = self.__class__.get_databases(pipeline_args)
        tablename = tools.sqlInterface.tables['hgm'][self.mode].__tablename__
        for genome, sqla_target in itertools.izip(*[hgm_args.genomes, self.output()]):
            df = parse_hgm_gtf(hgm_args.gtf_out_files[genome], genome)
            with tools.sqlite.ExclusiveSqlConnection(databases[genome]) as engine:
                df.to_sql(tablename, engine, if_exists='replace')
            sqla_target.touch()
            logger.info('Loaded table: {}.{}'.format(genome, tablename))


class IsoSeqIntronVectors(PipelineWrapperTask):
    """
    Constructs a database table representing all unique intron vectors from a IsoSeq dataset, based on the hints
    produced. This is a supplement to the homGeneMapping approach, but within the individual species in question.
    """
    @staticmethod
    def get_args(pipeline_args, genome):
        args = tools.misc.HashableNamespace()
        args.genome = genome
        args.hints_gff = BuildDb.get_args(pipeline_args, genome).hints_path
        return args

    def requires(self):
        pipeline_args = self.get_pipeline_args()
        for genome in pipeline_args.isoseq_genomes:
            yield self.clone(IsoSeqIntronVectorsDriverTask, genome=genome)


class IsoSeqIntronVectorsDriverTask(PipelineTask):
    """
    Driver task for IsoSeqIntronVectors
    """
    genome = luigi.Parameter()
    tablename = tools.sqlInterface.IsoSeqIntronIntervals.__tablename__

    def output(self):
        pipeline_args = self.get_pipeline_args()
        db = pipeline_args.dbs[self.genome]
        tools.fileOps.ensure_file_dir(db)
        conn_str = 'sqlite:///{}'.format(db)
        return luigi.contrib.sqla.SQLAlchemyTarget(connection_string=conn_str,
                                                   target_table=self.tablename,
                                                   update_id='_'.join([self.tablename, str(hash(pipeline_args))]))

    def requires(self):
        yield self.clone(AugustusPb)

    def construct_intervals(self, hints):
        """
        Converts the IsoSeq intron hints into groups of distinct intervals
        """
        lines = [x.split() for x in open(hints) if 'PB' in x]
        groups = collections.defaultdict(set)
        for l in lines:
            if l[2] != 'intron':
                continue
            attrs = dict([x.split('=') for x in l[-1].split(';')])
            if 'grp' not in attrs:  # not all introns get confidently assigned a group
                continue
            groups[attrs['grp']].add(tools.intervals.ChromosomeInterval(l[0], int(l[3]) - 1, int(l[4]), '.'))

        # isolate unique interval groups
        intervals = {frozenset(intervals) for intervals in groups.itervalues()}
        for i, interval_grp in enumerate(intervals):
            for interval in sorted(interval_grp):
                yield i, interval.chromosome, interval.start, interval.stop

    def run(self):
        pipeline_args = self.get_pipeline_args()
        intron_args = IsoSeqIntronVectors.get_args(pipeline_args, self.genome)
        df = pd.DataFrame(self.construct_intervals(intron_args.hints_gff))
        df.columns = ['SequenceId', 'chromosome', 'start', 'stop']
        with tools.sqlite.ExclusiveSqlConnection(pipeline_args.dbs[self.genome]) as engine:
            df.to_sql(self.tablename, engine, if_exists='replace')
        self.output().touch()
        logger.info('Loaded table {}.{}'.format(self.genome, self.tablename))


class AlignTranscripts(PipelineWrapperTask):
    """
    Aligns the transcripts from transMap/AugustusTMR/AugustusCGP to the parent transcript(s).
    """
    @staticmethod
    def get_args(pipeline_args, genome):
        base_dir = os.path.join(pipeline_args.work_dir, 'transcript_alignment')
        args = tools.misc.HashableNamespace()
        args.ref_genome = pipeline_args.ref_genome
        args.genome = genome
        args.ref_genome_fasta = GenomeFiles.get_args(pipeline_args, pipeline_args.ref_genome).fasta
        args.genome_fasta = GenomeFiles.get_args(pipeline_args, genome).fasta
        args.annotation_gp = ReferenceFiles.get_args(pipeline_args).annotation_gp
        args.ref_db_path = PipelineTask.get_database(pipeline_args, pipeline_args.ref_genome)
        # the alignment_modes members hold the input genePreds and the mRNA/CDS alignment output paths
        args.transcript_modes = {'transMap': {'gp': FilterTransMap.get_args(pipeline_args, genome).filtered_tm_gp,
                                              'mRNA': os.path.join(base_dir, genome + '.transMap.mRNA.psl'),
                                              'CDS': os.path.join(base_dir, genome + '.transMap.CDS.psl')}}
        if pipeline_args.augustus is True:
            args.transcript_modes['augTM'] = {'gp':  Augustus.get_args(pipeline_args, genome).augustus_tm_gp,
                                              'mRNA': os.path.join(base_dir, genome + '.augTM.mRNA.psl'),
                                              'CDS': os.path.join(base_dir, genome + '.augTM.CDS.psl')}
        if pipeline_args.augustus is True and genome in pipeline_args.rnaseq_genomes:
            args.transcript_modes['augTMR'] = {'gp': Augustus.get_args(pipeline_args, genome).augustus_tmr_gp,
                                               'mRNA': os.path.join(base_dir, genome + '.augTMR.mRNA.psl'),
                                               'CDS': os.path.join(base_dir, genome + '.augTMR.CDS.psl')}
        return args

    def validate(self):
        for tool in ['blat', 'pslCheck']:
            if not tools.misc.is_exec(tool):
                raise ToolMissingException('Tool {} not in global path.'.format(tool))

    def requires(self):
        self.validate()
        pipeline_args = self.get_pipeline_args()
        for target_genome in pipeline_args.target_genomes:
            yield self.clone(AlignTranscriptDriverTask, genome=target_genome)


class AlignTranscriptDriverTask(ToilTask):
    """
    Task for per-genome launching of a toil pipeline for aligning all transcripts found back to the reference in
    transcript space using BLAT.

    Each task returns a PSL of all alignments that will be analyzed next by EvaluateTranscripts.
    """
    genome = luigi.Parameter()

    def output(self):
        alignment_args = self.get_module_args(AlignTranscripts, genome=self.genome)
        for mode, paths in alignment_args.transcript_modes.iteritems():
            for aln_type in ['CDS', 'mRNA']:
                yield luigi.LocalTarget(paths[aln_type])

    def requires(self):
        alignment_args = self.get_module_args(AlignTranscripts, genome=self.genome)
        if 'augTM' in alignment_args.transcript_modes:
            yield self.clone(Augustus)
        yield self.clone(FilterTransMap)
        yield self.clone(ReferenceFiles)

    def run(self):
        logger.info('Launching Align Transcript toil pipeline for {} using {}.'.format(self.genome, self.batchSystem))
        toil_work_dir = os.path.join(self.work_dir, 'toil', 'transcript_alignment', self.genome)
        toil_options = self.prepare_toil_options(toil_work_dir)
        alignment_args = self.get_module_args(AlignTranscripts, genome=self.genome)
        align_transcripts(alignment_args, toil_options)
        logger.info('Align Transcript toil pipeline for {} completed.'.format(self.genome))


class EvaluateTranscripts(PipelineWrapperTask):
    """
    Evaluates all transcripts for important features. See the classify.py module for details on how this works.

    Each task will generate a genome-specific sqlite database. See the classify.py docstring for details.
    """
    @staticmethod
    def get_args(pipeline_args, genome):
        args = tools.misc.HashableNamespace()
        args.db_path = pipeline_args.dbs[genome]
        args.ref_db_path = PipelineTask.get_database(pipeline_args, pipeline_args.ref_genome)
        args.annotation_gp = ReferenceFiles.get_args(pipeline_args).annotation_gp
        args.fasta = GenomeFiles.get_args(pipeline_args, genome).fasta
        args.genome = genome
        args.ref_genome = pipeline_args.ref_genome
        # pass along all of the paths from alignment
        args.transcript_modes = AlignTranscripts.get_args(pipeline_args, genome).transcript_modes
        return args

    def validate(self):
        pass

    def requires(self):
        self.validate()
        pipeline_args = self.get_pipeline_args()
        for target_genome in pipeline_args.target_genomes:
            yield self.clone(EvaluateDriverTask, genome=target_genome)


class EvaluateDriverTask(PipelineTask):
    """
    Task for per-genome launching of a toil pipeline for aligning transcripts to their parent.
    """
    genome = luigi.Parameter()

    def build_table_names(self, eval_args):
        """construct table names based on input arguments"""
        tables = []
        for aln_mode in ['mRNA', 'CDS']:
            for tx_mode in eval_args.transcript_modes.iterkeys():
                names = [x.__tablename__ for x in tools.sqlInterface.tables[aln_mode][tx_mode].values()]
                tables.extend(names)
        return tables

    def pair_table_output(self, eval_args):
        """return dict of {table_name: SQLAlchemyTarget} for final writing"""
        return dict(zip(*[self.build_table_names(eval_args), self.output()]))

    def write_to_sql(self, results, eval_args):
        """Load the results into the SQLite database"""
        with tools.sqlite.ExclusiveSqlConnection(eval_args.db_path) as engine:
            for table, target in self.pair_table_output(eval_args).iteritems():
                df = results[table]
                df.to_sql(table, engine, if_exists='replace')
                target.touch()
                logger.info('Loaded table: {}.{}'.format(self.genome, table))

    def output(self):
        pipeline_args = self.get_pipeline_args()
        eval_args = self.get_module_args(EvaluateTranscripts, genome=self.genome)
        tools.fileOps.ensure_file_dir(eval_args.db_path)
        conn_str = 'sqlite:///{}'.format(eval_args.db_path)
        for table in self.build_table_names(eval_args):
            yield luigi.contrib.sqla.SQLAlchemyTarget(connection_string=conn_str,
                                                      target_table=table,
                                                      update_id='_'.join([table, str(hash(pipeline_args))]))

    def requires(self):
        return self.clone(AlignTranscripts), self.clone(ReferenceFiles)

    def run(self):
        logger.info('Evaluating transcript alignments for {}.'.format(self.genome))
        eval_args = self.get_module_args(EvaluateTranscripts, genome=self.genome)
        results = classify(eval_args)
        # results should be a dictionary of {table: dataframe}
        self.write_to_sql(results, eval_args)


class Consensus(PipelineWrapperTask):
    """
    Construct the consensus gene sets making use of the classification databases.
    """
    @staticmethod
    def get_args(pipeline_args, genome):
        base_dir = os.path.join(pipeline_args.out_dir, 'consensus_gene_set')
        # grab the genePred of every mode
        args = tools.misc.HashableNamespace()
        gp_list = [TransMap.get_args(pipeline_args, genome).tm_gp]
        args.tx_modes = ['transMap']
        args.denovo_tx_modes = []
        if pipeline_args.augustus is True:
            gp_list.append(Augustus.get_args(pipeline_args, genome).augustus_tm_gp)
            args.tx_modes.append('augTM')
        if pipeline_args.augustus is True and genome in pipeline_args.rnaseq_genomes:
            gp_list.append(Augustus.get_args(pipeline_args, genome).augustus_tmr_gp)
            args.tx_modes.append('augTMR')
        if pipeline_args.augustus_cgp is True:
            gp_list.append(AugustusCgp.get_args(pipeline_args).augustus_cgp_gp[genome])
            args.denovo_tx_modes.append('augCGP')
        if pipeline_args.augustus_pb is True and genome in pipeline_args.isoseq_genomes:
            gp_list.append(AugustusPb.get_args(pipeline_args, genome).augustus_pb_gp)
            args.denovo_tx_modes.append('augPB')
        args.gp_list = gp_list
        args.genome = genome
        args.transcript_modes = AlignTranscripts.get_args(pipeline_args, genome).transcript_modes.keys()
        args.augustus_cgp = pipeline_args.augustus_cgp
        args.db_path = pipeline_args.dbs[genome]
        args.ref_db_path = PipelineTask.get_database(pipeline_args, pipeline_args.ref_genome)
        args.hints_db_has_rnaseq = len(pipeline_args.rnaseq_genomes) > 0
        args.annotation_gp = ReferenceFiles.get_args(pipeline_args).annotation_gp
        args.consensus_gp = os.path.join(base_dir, genome + '.gp')
        args.consensus_gp_info = os.path.join(base_dir, genome + '.gp_info')
        args.consensus_gff3 = os.path.join(base_dir, genome + '.gff3')
        args.metrics_json = os.path.join(PipelineTask.get_metrics_dir(pipeline_args, genome), 'consensus.json')
        # user configurable options on how consensus finding should work
        args.intron_rnaseq_support = pipeline_args.intron_rnaseq_support
        args.exon_rnaseq_support = pipeline_args.exon_rnaseq_support
        args.intron_annot_support = pipeline_args.intron_annot_support
        args.exon_annot_support = pipeline_args.exon_annot_support
        args.original_intron_support = pipeline_args.original_intron_support
        args.denovo_num_introns = pipeline_args.denovo_num_introns
        args.denovo_splice_support = pipeline_args.denovo_splice_support
        args.denovo_exon_support = pipeline_args.denovo_exon_support
        args.minimum_coverage = pipeline_args.minimum_coverage
        args.require_pacbio_support = pipeline_args.require_pacbio_support
        args.in_species_rna_support_only = pipeline_args.in_species_rna_support_only
        return args

    def validate(self):
        pass

    def requires(self):
        self.validate()
        pipeline_args = self.get_pipeline_args()
        for target_genome in pipeline_args.target_genomes:
            yield self.clone(ConsensusDriverTask, genome=target_genome)


class ConsensusDriverTask(RebuildableTask):
    """
    Driver task for performing consensus finding.
    """
    genome = luigi.Parameter()

    def output(self):
        consensus_args = self.get_module_args(Consensus, genome=self.genome)
        yield luigi.LocalTarget(consensus_args.consensus_gp)
        yield luigi.LocalTarget(consensus_args.consensus_gp_info)
        yield luigi.LocalTarget(consensus_args.metrics_json)
        yield luigi.LocalTarget(consensus_args.consensus_gff3)

    def requires(self):
        pipeline_args = self.get_pipeline_args()
        yield self.clone(EvaluateTransMap)
        yield self.clone(EvaluateTranscripts)
        yield self.clone(Hgm)
        if pipeline_args.augustus_pb:
            yield self.clone(IsoSeqIntronVectors)

    def run(self):
        consensus_args = self.get_module_args(Consensus, genome=self.genome)
        logger.info('Generating consensus gene set for {}.'.format(self.genome))
        consensus_gp, consensus_gp_info, metrics_json, consensus_gff3 = self.output()
        metrics_dict = generate_consensus(consensus_args)
        PipelineTask.write_metrics(metrics_dict, metrics_json)


class Plots(RebuildableTask):
    """
    Produce final analysis plots
    """
    @staticmethod
    def get_args(pipeline_args):
        base_dir = os.path.join(pipeline_args.out_dir, 'plots')
        ordered_genomes = tools.hal.build_genome_order(pipeline_args.hal, pipeline_args.ref_genome,
                                                       genome_subset=pipeline_args.target_genomes)
        args = tools.misc.HashableNamespace()
        args.ordered_genomes = ordered_genomes
        # plots derived from transMap results
        args.tm_coverage = luigi.LocalTarget(os.path.join(base_dir, 'transmap_coverage.pdf'))
        args.tm_identity = luigi.LocalTarget(os.path.join(base_dir, 'transmap_identity.pdf'))
        # plots derived from transMap filtering
        args.paralogy = luigi.LocalTarget(os.path.join(base_dir, 'paralogy.pdf'))
        args.transmap_filtering = luigi.LocalTarget(os.path.join(base_dir, 'transmap_filtering.pdf'))
        # plots derived from transcript alignment / consensus finding
        args.coverage = luigi.LocalTarget(os.path.join(base_dir, 'coverage.pdf'))
        args.identity = luigi.LocalTarget(os.path.join(base_dir, 'identity.pdf'))
        args.completeness = luigi.LocalTarget(os.path.join(base_dir, 'completeness.pdf'))
        args.gene_failure = luigi.LocalTarget(os.path.join(base_dir, 'gene_failure.pdf'))
        args.transcript_failure = luigi.LocalTarget(os.path.join(base_dir, 'transcript_failure.pdf'))
        args.consensus_extrinsic_support = luigi.LocalTarget(os.path.join(base_dir, 'consensus_extrinsic_support.pdf'))
        args.consensus_annot_support = luigi.LocalTarget(os.path.join(base_dir, 'consensus_annotation_support.pdf'))
        args.tx_modes = luigi.LocalTarget(os.path.join(base_dir, 'transcript_modes.pdf'))
        args.indel = luigi.LocalTarget(os.path.join(base_dir, 'coding_indels.pdf'))
        # plots that depend on execution mode
        if pipeline_args.augustus is True:
            args.improvement = luigi.LocalTarget(os.path.join(base_dir, 'augustus_improvement.pdf'))
        if 'augCGP' in pipeline_args.modes or 'augPB' in pipeline_args.modes:
            args.denovo = luigi.LocalTarget(os.path.join(base_dir, 'denovo.pdf'))
        if 'augPB' in pipeline_args.modes:
            args.pb_support = luigi.LocalTarget(os.path.join(base_dir, 'IsoSeq_isoform_validation.pdf'))
            args.pb_genomes = pipeline_args.isoseq_genomes
        args.split_genes = luigi.LocalTarget(os.path.join(base_dir, 'split_genes.pdf'))
        # input data
        args.metrics_jsons = OrderedDict([[genome, Consensus.get_args(pipeline_args, genome).metrics_json]
                                          for genome in ordered_genomes])
        args.tm_jsons = OrderedDict([[genome, FilterTransMap.get_args(pipeline_args, genome).metrics_json]
                                     for genome in ordered_genomes])
        args.annotation_db = PipelineTask.get_database(pipeline_args, pipeline_args.ref_genome)
        args.dbs = OrderedDict([[genome, PipelineTask.get_database(pipeline_args, genome)]
                                for genome in ordered_genomes])
        args.in_species_rna_support_only = pipeline_args.in_species_rna_support_only
        return args

    def output(self):
        pipeline_args = self.get_pipeline_args()
        args = Plots.get_args(pipeline_args)
        return [p for p in args.__dict__.itervalues() if isinstance(p, luigi.LocalTarget)]

    def requires(self):
        yield self.clone(Consensus)

    def run(self):
        pipeline_args = self.get_pipeline_args()
        logger.info('Generating plots.')
        generate_plots(Plots.get_args(pipeline_args))


class AssemblyHub(PipelineWrapperTask):
    """
    Construct an assembly hub out of all the results
    """
    def requires(self):
        tools.fileOps.ensure_dir(self.out_dir)
        yield self.clone(CreateDirectoryStructure)
        yield self.clone(CreateTracks)


class CreateDirectoryStructure(PipelineTask):
    """
    Constructs the directory structure. Creates symlinks for all relevant files.
    """
    @staticmethod
    def get_args(pipeline_args):
        args = tools.misc.HashableNamespace()
        args.genomes = pipeline_args.hal_genomes
        args.out_dir = os.path.join(pipeline_args.out_dir, 'assemblyHub')
        args.hub_txt = os.path.join(args.out_dir, 'hub.txt')
        args.genomes_txt = os.path.join(args.out_dir, 'genomes.txt')
        args.groups_txt = os.path.join(args.out_dir, 'groups.txt')
        genome_files = frozendict({genome: GenomeFiles.get_args(pipeline_args, genome)
                                   for genome in pipeline_args.hal_genomes})
        sizes = {}
        twobits = {}
        trackdbs = {}
        for genome, genome_file in genome_files.iteritems():
            sizes[genome] = (genome_file.sizes, os.path.join(args.out_dir, genome, 'chrom.sizes'))
            twobits[genome] = (genome_file.two_bit, os.path.join(args.out_dir, genome, '{}.2bit'.format(genome)))
            trackdbs[genome] = os.path.join(args.out_dir, genome, 'trackDb.txt')
        args.sizes = frozendict(sizes)
        args.twobits = frozendict(twobits)
        args.trackdbs = frozendict(trackdbs)
        args.hal = os.path.join(args.out_dir, os.path.basename(pipeline_args.hal))
        return args

    def requires(self):
        yield self.clone(Consensus)

    def output(self):
        pipeline_args = self.get_pipeline_args()
        args = CreateDirectoryStructure.get_args(pipeline_args)
        yield luigi.LocalTarget(args.hub_txt)
        yield luigi.LocalTarget(args.genomes_txt)
        yield luigi.LocalTarget(args.groups_txt)
        yield luigi.LocalTarget(args.hal)
        for local_path, hub_path in args.sizes.itervalues():
            yield luigi.LocalTarget(hub_path)
        for local_path, hub_path in args.twobits.itervalues():
            yield luigi.LocalTarget(hub_path)

    def run(self):
        pipeline_args = self.get_pipeline_args()
        args = CreateDirectoryStructure.get_args(pipeline_args)
        tools.fileOps.ensure_file_dir(args.out_dir)

        # write the hub.txt file
        hal_name = os.path.splitext(os.path.basename(pipeline_args.hal))[0]
        with luigi.LocalTarget(args.hub_txt).open('w') as outf:
            outf.write(hub_str.format(hal_name))

        # write the groups.txt file
        with luigi.LocalTarget(args.groups_txt).open('w') as outf:
            outf.write(groups_str)

        # write the genomes.txt file, construct a dir
        # TODO: include ancestors? Provide a flag?
        with luigi.LocalTarget(args.genomes_txt).open('w') as outf:
            for genome, (sizes_local_path, sizes_hub_path) in args.sizes.iteritems():
                outf.write(genome_str.format(genome, find_default_pos(sizes_local_path)))

        # link the hal
        os.link(pipeline_args.hal, args.hal)

        # construct a directory for each genome
        for genome, (sizes_local_path, sizes_hub_path) in args.sizes.iteritems():
            tools.fileOps.ensure_file_dir(sizes_hub_path)
            os.link(sizes_local_path, sizes_hub_path)
            twobit_local_path, twobit_hub_path = args.twobits[genome]
            os.link(twobit_local_path, twobit_hub_path)

        # construct the initial trackDb with the HAL tracks
        # TODO: this will include self tracks. Provide a flag?
        org_str = construct_org_str(args.genomes)
        for genome, track_db in args.trackdbs.iteritems():
            with luigi.LocalTarget(track_db).open('w') as outf:
                outf.write(snake_composite.format(org_str=org_str))
                for genome in args.genomes:
                    # by default, only the reference genome is visible
                    visibility = 'hide' if genome != pipeline_args.ref_genome else 'full'
                    outf.write(snake_template.format(genome=genome, hal_path='../{}'.format(hal_name),
                                                     visiblity=visibility))


class CreateTracks(PipelineWrapperTask):
    """
    Wrapper task for track creation.
    """
    @staticmethod
    def get_args(pipeline_args, genome):
        args = tools.misc.HashableNamespace()
        directory_args = CreateDirectoryStructure.get_args(pipeline_args)
        cgp_args = AugustusCgp.get_args(pipeline_args)
        ref_args = ReferenceFiles.get_args(pipeline_args)
        args.fasta = GenomeFiles.get_args(pipeline_args, genome).fasta
        args.trackDb = directory_args.trackdbs[genome]
        out_dir = os.path.join(directory_args.out_dir, genome)
        args.annotation_gp = ref_args.annotation_gp
        if pipeline_args.augustus_cgp is True:
            args.augustus_cgp_gp = cgp_args.augustus_cgp_gp[pipeline_args.ref_genome]
            args.cgp_track = luigi.LocalTarget(os.path.join(out_dir, 'augustus_cgp.bb'))
            args.denovo_tx_modes = ['augCGP']
        # for the reference genome, we only have the reference genePred and CGP if it was ran
        if genome == pipeline_args.ref_genome:
            args.annotation_track = luigi.LocalTarget(os.path.join(out_dir, 'annotation.bb'))
        else:
            consensus_args = Consensus.get_args(pipeline_args, genome)
            args.tx_modes = consensus_args.tx_modes
            args.consensus_gp = consensus_args.consensus_gp
            args.consensus_gp_info = consensus_args.consensus_gp_info
            args.consensus_track = luigi.LocalTarget(os.path.join(out_dir, 'consensus.bb'))
            args.evaluation_track = luigi.LocalTarget(os.path.join(out_dir, 'evaluation.bb'))
            tm_args = TransMap.get_args(pipeline_args, genome)
            args.tm_psl = tm_args.tm_psl
            args.tm_gp = tm_args.tm_gp
            args.tm_track = luigi.LocalTarget(os.path.join(out_dir, 'transmap.bb'))
            args.filtered_tm_gp = FilterTransMap.get_args(pipeline_args, genome).filtered_tm_gp
            args.filtered_tm_track = luigi.LocalTarget(os.path.join(out_dir, 'filtered_transmap.bb'))
            if pipeline_args.augustus is True and genome in pipeline_args.rnaseq_genomes:
                aug_args = Augustus.get_args(pipeline_args, genome)
                args.augustus_tm_gp = aug_args.augustus_tm_gp
                if genome in pipeline_args.rnaseq_genomes:
                    args.augustus_tmr_gp = aug_args.augustus_tmr_gp
                else:
                    args.augustus_tmr_gp = None
                args.augustus_track = os.path.join(out_dir, 'augustus.bb')
        # TODO: isoseq_genomes cannot contain reference, but probably should
        if genome in pipeline_args.isoseq_genomes:
            args.isoseq_bams = {bam: os.path.join(out_dir, os.path.basename(bam))
                                for bam in pipeline_args.cfg['ISO_SEQ_BAM'][genome]}
            if augustus_pb is True:
                args.denovo_tx_modes.append('augPB')
                args.augustus_pb_gp = AugustusPb.get_args(pipeline_args, genome).augustus_pb_gp
                args.augustus_pb_track = luigi.LocalTarget(os.path.join(out_dir, 'aug_pb.bb'))
        if genome in pipeline_args.rnaseq_genomes:
            args.bams = {'BAM': pipeline_args.cfg['BAM'][genome], 'INTRONBAM': pipeline_args.cfg['INTRONBAM'][genome]}
            args.splice_track = luigi.LocalTarget(os.path.join(args.out_dir, 'splices.bb'))
            if len(args.bams['BAM']) > 0:
                args.max_expression_track = luigi.LocalTarget(os.path.join(args.out_dir, 'max_expression.bw'))
                args.median_expression_track = luigi.LocalTarget(os.path.join(args.out_dir, 'median_expression.bw'))
        args.hints = BuildDb.get_args(pipeline_args, genome).hints_path
        args.chrom_sizes = GenomeFiles.get_args(pipeline_args, genome).sizes
        args.db = pipeline_args.dbs[genome]
        args.ref_db = pipeline_args.dbs[pipeline_args.ref_genome]
        return args

    def validate(self):
        for tool in ['bedSort', 'pslToBigPsl', 'wiggletools', 'wigToBigWig']:
            if not tools.misc.is_exec(tool):
                raise ToolMissingException('Tool {} not in global path.'.format(tool))

    def requires(self):
        pipeline_args = self.get_pipeline_args()
        self.validate()
        for genome in pipeline_args.target_genomes:
            yield self.clone(CreateTracksDriverTask, genome=genome)
        yield self.clone(CreateTracksDriverTask, genome=pipeline_args.ref_genome)


class CreateTracksDriverTask(RebuildableTask):
    """
    Task for track creation. Tracks include:

    1) Consensus. Create custom bigGenePred tracks out of the consensus.
    2) transMap.
    3) Filtered transMap.
    4) AugustusCGP. (if it exists)
    5) AugustusPB. (if it exists)
    6) AugustusTMR. (if it exists).
    7) Errors. Use the evaluation database to extract the indel tracks.
    8) Average RNA-seq expression.
    9) Maximum RNA-seq expression.
    10) Splice junctions.
    11) IsoSeq BAMs (if they exist).
    """
    genome = luigi.Parameter()

    def construct_consensus_track(self, consensus_gp, consensus_gp_info, chrom_sizes, out_bb):
        """Converts the consensus track."""
        def find_rgb(info):
            """red for failed, blue for coding, green for non-coding"""
            if info.failed_gene == 'True':
                return '212,76,85'
            elif info.transcript_biotype == 'protein_coding':
                return '76,85,212'
            return '85,212,76'

        consensus_gp = tools.transcripts.gene_pred_iterator(consensus_gp)
        has_pb = 'pacbio_isoform_supported' in consensus_gp_info.columns
        # start constructing the strings. This requires mucking around with the genePred format.
        with tools.fileOps.TemporaryFilePath() as tmp_gp, tools.fileOps.TemporaryFilePath() as as_file:
            with open(tmp_gp, 'w') as outf:
                for tx in consensus_gp:
                    info = consensus_gp_info.ix[tx.name]
                    block_starts, block_sizes, exon_frames = create_bed_info_gp(tx)
                    row = [tx.chromosome, tx.start, tx.stop, tx.name, tx.score, tx.strand, tx.thick_start, tx.thick_stop,
                           find_rgb(info), tx.block_count, block_sizes, block_starts, info.transcript_name,
                           tx.cds_start_stat, tx.cds_end_stat, exon_frames, info.transcript_biotype, tx.name2,
                           info.source_gene_common_name, info.gene_biotype, info.source_gene, info.source_transcript,
                           info.alignment_id, info.alternative_source_transcripts, info.exon_annotation_support,
                           info.exon_rna_support, info.failed_gene, info.frameshift, info.intron_annotation_support,
                           info.intron_rna_support, info.transcript_class, info.transcript_modes]
                    if has_pb:
                        row.append(info.pacbio_isoform_supported)
                    tools.fileOps.print_row(row, outf)
            with open(as_file, 'w') as outf:
                as_str = construct_consensus_gp_as(has_pb)
                outf.write(as_str)
            tools.procOps.run_proc(['bedSort', tmp_gp, tmp_gp])
            cmd = ['bedToBigBed', '-extraIndex=name,name2,sourceGene,sourceTranscript,geneName,geneName2',
                   '-typebed12+20', '-tab', '-as={}'.format(as_file), tmp_gp, chrom_sizes, out_bb]
            tools.procOps.run_proc(cmd)

    def construct_modified_bgp_track(self, gp, info, chrom_sizes, out_bb):
        """Converts the reference annotation or filtered transMap to a modified bigGenePred"""
        def find_rgb(info):
            """blue for coding, green for non-coding"""
            if info.transcript_biotype == 'protein_coding':
                return '76,85,212'
            return '85,212,76'

        info = info.reset_index().set_index(['TranscriptId'])
        gp = tools.transcripts.gene_pred_iterator(gp)
        with tools.fileOps.TemporaryFilePath() as tmp, tools.fileOps.TemporaryFilePath() as as_file:
            with open(as_file, 'w') as outf:
                outf.write(modified_bgp_as)
            with open(tmp, 'w') as outf:
                for tx in gp:
                    s = info.ix[tx.name]
                    block_starts, block_sizes, exon_frames = create_bed_info_gp(tx)
                    row = [tx.chromosome, tx.start, tx.stop, tx.name, tx.score, tx.strand, tx.thick_start,
                           tx.thick_stop, find_rgb(s), tx.block_count, block_sizes, block_starts,
                           s.TranscriptName, tx.cds_start_stat, tx.cds_end_stat, exon_frames,
                           s.GeneName, s.GeneId, s.TranscriptBiotype, s.GeneBiotype]
                    tools.fileOps.print_row(row, outf)
            tools.procOps.run_proc(['bedSort', tmp, tmp])
            cmd = ['bedToBigBed', '-extraIndex=name,name2,geneId,geneName',
                   '-typebed12+8', '-tab', '-as={}'.format(as_file), tmp, chrom_sizes, out_bb]
            tools.procOps.run_proc(cmd)

    def construct_transmap_psl_track(self, transmap_psl, fasta, tm_gp, annotation_gp, chrom_sizes, out_bb):
        """Creates a bigPsl out of the transmap PSL"""
        with tools.fileOps.TemporaryFilePath() as tmp, tools.fileOps.TemporaryFilePath() as as_file, \
             tools.fileOps.TemporaryFilePath() as cds, tools.fileOps.TemporaryFilePath() as mrna:
            seq_dict = tools.bio.get_sequence_dict(fasta)
            ref_tx_dict = tools.transcripts.get_gene_pred_dict(annotation_gp)
            for tx in tools.transcripts.gene_pred_iterator(tm_gp):
                ref_tx = ref_tx_dict[tools.nameConversions.strip_alignment_numbers(tx.name)]
                tools.bio.write_fasta(mrna, tx.name, str(seq_dict[ref_tx.name]))
                start = ref_tx.cds_coordinate_to_mrna(0) + ref_tx.offset
                stop = start + ref_tx.cds_size - ((ref_tx.cds_size - ref_tx.offset) % 3)
                cds.write('{}\t{}..{}\n'.format(tx.name, start + 1, stop))

            cmd = [['pslToBigPsl', '-cds={}'.format(cds), '-fa={}'.format(mrna), transmap_psl, 'stdout'],
                   ['sort', '-k1,1', '-k2,2n']]
            tools.procOps.run_proc(cmd, stdout=tmp)
            with open(as_file, 'w') as outf:
                outf.write(bigpsl)
            cmd = ['bedToBigBed', '-type=bed12+13', '-tab', '-extraIndex=name',
                   '-as={}'.format(as_file), tmp, chrom_sizes, out_bb]
            tools.procOps.run_proc(cmd)

    def construct_denovo_track(self, augustus_gp, alt_names, annotation_info, chrom_sizes, out_bb):
        """Creates a bigGenePred-like track out of the denovo predictions"""
        def find_rgb(s):
            if s.AssignedGeneId is None and s.AlternativeGeneIds is None:
                return '175,87,207'  # both null -> purple (denovo)
            elif s.AssignedGeneId is None and s.AlternativeGeneIds is not None:
                return '87,207,175'  # no assigned -> teal (possible_paralog)
            return '0'

        def find_alternative_gene_names(s, annotation_info):
            r = {annotation_info.ix[gene].iloc[0].GeneName for gene in s.AlternativeGeneIds.split(',')}
            return ','.join(r)

        augustus_gp = tools.transcripts.gene_pred_iterator(augustus_gp)
        annotation_info = annotation_info.reset_index().set_index(['GeneId'])
        with tools.fileOps.TemporaryFilePath() as tmp, tools.fileOps.TemporaryFilePath() as as_file:
            with open(as_file, 'w') as outf:
                outf.write(denovo_as)
            with open(tmp, 'w') as outf:
                for tx in augustus_gp:
                    s = alt_names.ix[tx.name]
                    if s.AssignedGeneId is None:
                        assigned_gene_id = gene_name = gene_type = alternative_gene_names = 'N/A'
                    else:
                        a = annotation_info.ix[s.AssignedGeneId].iloc[0]
                        gene_name = a.GeneName
                        gene_type = a.GeneBiotype
                        assigned_gene_id = a.index[0]
                        alternative_gene_names = find_alternative_gene_names(s, a)
                    block_starts, block_sizes, exon_frames = create_bed_info_gp(tx)
                    row = [tx.chromosome, tx.start, tx.stop, tx.name, tx.score, tx.strand, tx.thick_start,
                           tx.thick_stop, find_rgb(s), tx.block_count, block_sizes, block_starts,
                           gene_name, tx.cds_start_stat, tx.cds_end_stat, exon_frames,
                           gene_type, assigned_gene_id, s.AlternativeGeneIds, alternative_gene_names]
                    tools.fileOps.print_row(row, outf)
        tools.procOps.run_proc(['bedSort', tmp, tmp])
        cmd = ['bedToBigBed', '-extraIndex=geneName,assignedGeneId,name,name2',
               '-typebed12+8', '-tab', '-as={}'.format(as_file), tmp, chrom_sizes, out_bb]
        tools.procOps.run_proc(cmd)

    def construct_tmr_track(self, tm_gp, annotation_info, chrom_sizes, out_bb, tmr_gp=None):
        """Constructs a bigGenePred-like track out of the transMap-derived predictions. Combines them"""
        annotation_info = annotation_info.reset_index().set_index(['TranscriptId'])
        for gp, color in zip(*[[tm_gp, tmr_gp], ['38,112,75', '112,38,75']]):
            if gp is None:
                continue
            gp = tools.transcripts.gene_pred_iterator(gp)
            with tools.fileOps.TemporaryFilePath() as tmp, tools.fileOps.TemporaryFilePath() as as_file:
                with open(as_file, 'w') as outf:
                    outf.write(modified_bgp_as)
                with open(tmp, 'w') as outf:
                    for tx in gp:
                        s = annotation_info.ix[tx.name]
                        block_starts, block_sizes, exon_frames = create_bed_info_gp(tx)
                        row = [tx.chromosome, tx.start, tx.stop, tx.name, tx.score, tx.strand, tx.thick_start,
                               tx.thick_stop, color, tx.block_count, block_sizes, block_starts,
                               s.TranscriptName, tx.cds_start_stat, tx.cds_end_stat, exon_frames,
                               s.GeneName, s.GeneId, s.TranscriptBiotype, s.GeneBiotype]
                        tools.fileOps.print_row(row, outf)
                tools.procOps.run_proc(['bedSort', tmp, tmp])
                cmd = ['bedToBigBed', '-extraIndex=geneId,assignedGeneId,name2',
                       '-typebed12+8', '-tab', '-as={}'.format(as_file), tmp, chrom_sizes, out_bb]
                tools.procOps.run_proc(cmd)

    def construct_error_track(self, consensus_gp_info, db_path, tx_modes, chrom_sizes, out_bb):
        """Loads the error tracks from the database"""
        def load_evals(tx_mode):
            cds_table = tools.sqlInterface.tables['CDS'][tx_mode]['evaluation']
            mrna_table = tools.sqlInterface.tables['mRNA'][tx_mode]['evaluation']
            cds_df = pd.read_sql_table(cds_table.__tablename__, engine).set_index('AlignmentId')
            mrna_df = pd.read_sql_table(mrna_table.__tablename__, engine).set_index('AlignmentId')
            return {'CDS': cds_df, 'mRNA': mrna_df}

        engine = tools.sqlInterface.create_engine('sqlite:///' + db_path)
        evals = {tx_mode: load_evals(tx_mode) for tx_mode in tx_modes}
        aln_ids = set(consensus_gp_info.AlignmentId)
        rows = []
        for aln_id in aln_ids:
            tx_mode = tools.nameConversions.alignment_type(aln_id)
            df = tools.misc.slice_df(evals[tx_mode]['CDS'], aln_id)
            if len(df) == 0:
                df = tools.misc.slice_df(evals[tx_mode]['mRNA'], aln_id)
            for _, s in df.iterrows():
                rows.append([s.chromosome, s.start, s.stop, s.classifier, 0, s.strand])
        with tools.fileOps.TemporaryFilePath() as tmp:
            tools.fileOps.print_rows(tmp, rows)
            tools.procOps.run_proc(['bedSort', tmp, tmp])
            cmd = ['bedToBigBed', '-tab', tmp, chrom_sizes, out_bb]
            tools.procOps.run_proc(cmd)

    def construct_splice_track(self, hints_gff, chrom_sizes, out_bb):
        """Constructs splice intervals out of the hints gff"""
        def parse_entry(entry):
            """Converts a GFF entry to BED12"""
            start = int(entry[3]) - 1
            stop = int(entry[4])
            block_starts = '0,{}'.format(stop - start - 2)
            mult = tools.misc.parse_gff_attr_line(entry[-1]).get('mult', 1)
            return [entry[0], start, stop, 'SpliceJunction', mult, '.', start, stop, '204,124,45',
                    '2', '2,2', block_starts]

        entries = []
        for line in open(hints_gff):
            if '\tintron\t' in line and 'src=E' in line:
                entries.append(parse_entry(line.split('\t')))
        mults = map(int, [x[4] for x in entries])
        tot = sum(mults)
        # calculate junctions per million
        jpm = [1.0 * x * 10 ** 6 / tot for x in mults]
        # log scale
        log_jpm = map(np.log, jpm)
        # convert to 1-1000 scale
        # http://stackoverflow.com/questions/929103/convert-a-number-range-to-another-range-maintaining-ratio
        old_min = min(log_jpm)
        old_max = max(log_jpm)
        old_range = old_max - old_min
        scaled_log_jpm = map(lambda x: int(round((((x - old_min) * 999) / old_range) + 1)), log_jpm)
        for e, s in zip(*[entries, scaled_log_jpm]):
            e[4] = s

        # load to file
        with tools.fileOps.TemporaryFilePath() as tmp:
            tools.fileOps.print_rows(tmp, entries)
            tools.procOps.run_proc(['bedSort', tmp, tmp])
            cmd = ['bedToBigBed', '-tab', tmp, chrom_sizes, out_bb]
            tools.procOps.run_proc(cmd)

    def construct_expression_track(self, bams, chrom_sizes, out_median_bw, out_max_bw):
        """Constructs a wiggle expression track out of the non-intron bams"""
        cmd = [['wiggletools', 'median'] + bams,
               ['wigToBigWig', '-clip', '/dev/stdin', chrom_sizes, out_median_bw]]
        tools.procOps.run_proc(cmd)
        cmd = [['wiggletools', 'max'] + bams,
               ['wigToBigWig', '-clip', '/dev/stdin', chrom_sizes, out_max_bw]]
        tools.procOps.run_proc(cmd)

    def output(self):
        pipeline_args = self.get_pipeline_args()
        args = CreateTracks.get_args(pipeline_args, self.genome)
        for item in args.__dict__.itervalues():
            if isinstance(item, luigi.LocalTarget):
                yield item

    def run(self):
        pipeline_args = self.get_pipeline_args()
        args = CreateTracks.get_args(pipeline_args, self.genome)
        alt_names = load_alt_names(args.db_paths, args.denovo_tx_modes)
        annotation_info = tools.sqlInterface.load_annotation(args.ref_db)
        with open(args.trackDb, 'a') as outf:
            if pipeline_args.augustus_cgp is True:
                self.construct_denovo_track(args.augustus_cgp_gp, alt_names, annotation_info, args.chrom_sizes,
                                            args.cgp_track)
                outf.write(denovo_template.format(name='augustus_cgp_{}'.format(self.genome), short_label='AugustusCGP',
                                                  long_label='AugustusCGP', description='Comparative Augustus',
                                                  path=os.path.basename(args.cgp_track)))

            if 'isoseq_bams' in args:
                outf.write(bam_composite_template.format(genome=self.genome))
                for bam, new_bam in args.isoseq_bams.iteritems():
                    os.link(bam, new_bam)
                    outf.write(bam_template.format(bam=new_bam, genome=self.genome))

            if 'bams' in args:
                if len(args.bams['BAM']) > 0:
                    outf.write(wiggle_template.format(genome=self.genome, mode='median',
                                                      path=os.path.basename(args.median_expression_track)))
                    outf.write(wiggle_template.format(genome=self.genome, mode='maximum',
                                                      path=os.path.basename(args.maximum_expression_track)))
                    self.construct_expression_track(args.bams['BAM'], args.chrom_sizes, args.median_expression_track,
                                                    args.maximum_expression_track)
                outf.write(splice_template.format(genome=self.genome, path=os.path.basename(args.splice_track)))
                self.construct_splice_track(args.hints, args.chrom_sizes, args.splice_track)

            if self.genome == pipeline_args.ref_genome:
                annotation_name = os.path.splitext(os.path.basename(args.annotation_gp))[0]
                outf.write(bgp_template.format(name='annotation_{}'.format(self.genome), label=annotation_name,
                                               path=os.path.basename(args.annotation_track)))
                self.construct_modified_bgp_track(args.annotation_gp, annotation_info, args.chrom_sizes, 
                                                  args.annotation_track)
            else:
                consensus_gp_info = pd.read_csv(args.consensus_gp_info, sep='\t', header=0).set_index('transcript_id')
                outf.write(consensus_template.format(genome=self.genome, path=os.path.basename(args.consensus_track)))
                self.construct_consensus_track(args.consensus_gp, consensus_gp_info, args.chrom_sizes, 
                                               args.consensus_track)
                outf.write(error_template.format(genome=self.genome, path=os.path.basename(args.evaluation_track)))
                self.construct_error_track(consensus_gp_info, args.db, args.tx_modes, args.chrom_sizes,
                                           args.evaluation_track)
                outf.write(bigpsl_template.format(name='transmap_{}'.format(self.genome), shortLabel='transMap',
                                                  longLabel='transMap', path=os.path.basename(args.tm_track),
                                                  visibility='pack'))
                self.construct_transmap_psl_track(args.tm_psl, args.fasta, args.tm_gp, args.annotation_gp,
                                                  args.chrom_sizes, args.tm_track)
                outf.write(bgp_template.format(name='filtered_tm_{}'.format(self.genome),
                                               label='Filtered transMap',
                                               path=os.path.basename(args.filtered_tm_track)))
                self.construct_transmap_psl_track(args.filtered_tm_gp, args.chrom_sizes, args.tm_track)
                if 'augustus_tm_gp' in args:
                    self.construct_tmr_track(args.tm_gp, annotation_info, args.chrom_sizes, args.augustus_track,
                                             args.augustus_tmr_gp)
                if 'augustus_pb_gp' in args:
                    self.construct_denovo_track(args.augustus_pb_gp, alt_names, annotation_info, args.chrom_sizes,
                                                args.augustus_pb_track)



###
# assembly hub functions, including generating autoSql files
###


def create_bed_info_gp(gp):
    """Creates the block_starts, block_sizes and exon_frames fields from a GenePredTranscript object"""
    block_starts = ','.join(map(str, gp.block_starts))
    block_sizes = ','.join(map(str, gp.block_sizes))
    exon_frames = ','.join(map(str, gp.exon_frames))
    return block_starts, block_sizes, exon_frames


def find_default_pos(chrom_sizes, window_size=30000):
    """
    Returns a window_size window over the beginning of the largest chromosome
    :param chrom_sizes: chrom sizes file
    :param window_size: window size to extend from
    :return: string
    """
    sizes = [x.split() for x in open(chrom_sizes)]
    sorted_sizes = sorted(sizes, key=lambda (chrom, size): -int(size))
    return '{}:{}-{}'.format(sorted_sizes[0][0], 1, window_size)


def construct_org_str(genomes):
    """Constructs the organism string for the hal snakes. format is genome=genome space separated"""
    return ' '.join(['{0}={0}'.format(genome) for genome in genomes])


def construct_consensus_gp_as(has_pb):
    """Dynamically generate an autosql file for consensus"""
    consensus_gp_as = '''table bigCat
"bigCat gene models"
    (
    string chrom;       "Reference sequence chromosome or scaffold"
    uint   chromStart;  "Start position in chromosome"
    uint   chromEnd;    "End position in chromosome"
    string name;        "Name or ID of item, ideally both human readable and unique"
    uint score;         "Score (0-1000)"
    char[1] strand;     "+ or - for strand"
    uint thickStart;    "Start of where display should be thick (start codon)"
    uint thickEnd;      "End of where display should be thick (stop codon)"
    uint reserved;       "RGB value (use R,G,B string in input file)"
    int blockCount;     "Number of blocks"
    int[blockCount] blockSizes; "Comma separated list of block sizes"
    int[blockCount] chromStarts; "Start positions relative to chromStart"
    string name2;       "Transcript name"
    string cdsStartStat; "Status of CDS start annotation (none, unknown, incomplete, or complete)"
    string cdsEndStat;   "Status of CDS end annotation (none, unknown, incomplete, or complete)"
    int[blockCount] exonFrames; "Exon frame {0,1,2}, or -1 if no frame for exon"
    string type;        "Transcript type"
    string geneName;    "Gene ID"
    string geneName2;   "Gene name"
    string geneType;    "Gene type"
    string sourceGene;    "Source gene ID"
    string sourceTranscript;    "The source transcript ID"
    string alignmentId;  "Original alignment ID"
    string alternativeSourceTranscripts;    "Alternative source transcripts"
    string exonAnnotationSupport;   "Exon support in reference annotation"
    string exonRnaSupport;  "RNA exon support"
    bool failedGene;   "Did this gene fail the identity cutoff?"
    bool frameshift;  "Frameshifted relative to source?"
    string intronAnnotationSupport;   "Intron support in reference annotation"
    string intronRnaSupport;   "RNA intron support"
    string transcriptClass;    "Transcript class"
    string transcriptModes;    "Transcript mode(s)"
'''
    if has_pb:
        consensus_gp_as += '    string pbIsoformSupported;   "Is this transcript supported by IsoSeq?"'
    consensus_gp_as += '\n)\n'
    return consensus_gp_as


modified_bgp_as = '''table bigGenePred
"bigGenePred gene models"
    (
    string chrom;       "Reference sequence chromosome or scaffold"
    uint   chromStart;  "Start position in chromosome"
    uint   chromEnd;    "End position in chromosome"
    string name;        "Name or ID of item, ideally both human readable and unique"
    uint score;         "Score (0-1000)"
    char[1] strand;     "+ or - for strand"
    uint thickStart;    "Start of where display should be thick (start codon)"
    uint thickEnd;      "End of where display should be thick (stop codon)"
    uint reserved;       "RGB value (use R,G,B string in input file)"
    int blockCount;     "Number of blocks"
    int[blockCount] blockSizes; "Comma separated list of block sizes"
    int[blockCount] chromStarts; "Start positions relative to chromStart"
    string name2;       "Transcript name"
    string cdsStartStat; "Status of CDS start annotation (none, unknown, incomplete, or complete)"
    string cdsEndStat;   "Status of CDS end annotation (none, unknown, incomplete, or complete)"
    int[blockCount] exonFrames; "Exon frame {0,1,2}, or -1 if no frame for exon"
    string geneId;    "Gene ID"
    string geneName;   "Gene name"
    string type;        "Transcript type"
    string geneType;    "Gene type"
    )

'''


denovo_as = '''table denovo
"denovo gene models"
    (
    string chrom;       "Reference sequence chromosome or scaffold"
    uint   chromStart;  "Start position in chromosome"
    uint   chromEnd;    "End position in chromosome"
    string name;        "Name or ID of item, ideally both human readable and unique"
    uint score;         "Score (0-1000)"
    char[1] strand;     "+ or - for strand"
    uint thickStart;    "Start of where display should be thick (start codon)"
    uint thickEnd;      "End of where display should be thick (stop codon)"
    uint reserved;       "RGB value (use R,G,B string in input file)"
    int blockCount;     "Number of blocks"
    int[blockCount] blockSizes; "Comma separated list of block sizes"
    int[blockCount] chromStarts; "Start positions relative to chromStart"
    string name2;       "Assigned gene name"
    string cdsStartStat; "Status of CDS start annotation (none, unknown, incomplete, or complete)"
    string cdsEndStat;   "Status of CDS end annotation (none, unknown, incomplete, or complete)"
    int[blockCount] exonFrames; "Exon frame {0,1,2}, or -1 if no frame for exon"
    string geneType;    "Assigned gene type"
    string assignedGeneId; "Assigned source gene ID"
    string alternativeGeneIds; "Alternative source gene IDs"
    string alternativeGeneNames; "Alternative source gene names"
'''


bigpsl = '''table bigPsl
"bigPsl pairwise alignment"
    (
    string chrom;       "Reference sequence chromosome or scaffold"
    uint   chromStart;  "Start position in chromosome"
    uint   chromEnd;    "End position in chromosome"
    string name;        "Name or ID of item, ideally both human readable and unique"
    uint score;         "Score (0-1000)"
    char[1] strand;     "+ or - indicates whether the query aligns to the + or - strand on the reference"
    uint thickStart;    "Start of where display should be thick (start codon)"
    uint thickEnd;      "End of where display should be thick (stop codon)"
    uint reserved;       "RGB value (use R,G,B string in input file)"
    int blockCount;     "Number of blocks"
    int[blockCount] blockSizes; "Comma separated list of block sizes"
    int[blockCount] chromStarts; "Start positions relative to chromStart"
    uint    oChromStart;"Start position in other chromosome"
    uint    oChromEnd;  "End position in other chromosome"
    char[1] oStrand;    "+ or -, - means that psl was reversed into BED-compatible coordinates"
    uint    oChromSize; "Size of other chromosome."
    int[blockCount] oChromStarts; "Start positions relative to oChromStart or from oChromStart+oChromSize depending on strand"
    lstring  oSequence;  "Sequence on other chrom (or edit list, or empty)"
    string   oCDS;       "CDS in NCBI format"
    uint    chromSize;"Size of target chromosome"
    uint match;        "Number of bases matched."
    uint misMatch; " Number of bases that don't match "
    uint repMatch; " Number of bases that match but are part of repeats "
    uint nCount;   " Number of 'N' bases "
    uint seqType;    "0=empty, 1=nucleotide, 2=amino_acid"
    )

'''


###
# Templates for trackDb entries
###


hub_str = '''hub {hal}
shortLabel {hal}
longLabel {hal}
genomesFile genomes.txt
email NoEmail

'''

genome_str = '''genome {genome}
twoBitPath {genome}/{genome}.2bit
trackDb {genome}/trackDb.txt
organism {genome}
description {genome}
scientificName {genome}
defaultPos {default_pos}
groups groups.txt

'''


groups_str = '''name cat_tracks
label Comparative Annotation Toolkit
priority 1
defaultIsClosed 0

name snake
label Alignment Snakes
priority 2
defaultIsClosed 0

'''


snake_composite = '''track hubCentral
compositeTrack on
shortLabel Cactus
longLabel my hub
group cat_tracks
subGroup1 view Track_Type Snake=Alignments
subGroup2 orgs Organisms {org_str}
dragAndDrop subTracks
dimensions dimensionX=view dimensionY=orgs
noInherit on
priority 0
centerLabelsDense on
visibility full
type bigBed 3

    track hubCentralAlignments
    shortLabel Alignments
    view Alignments
    visibility full
    subTrack hubCentral

'''

snake_template = '''        track snake_{genome}
        longLabel {genome}
        shortLabel {genome}
        otherSpecies {genome}
        visibility {visibility}
        parent hubCentralAlignments off
        priority 2
        bigDataUrl {hal_path}
        type halSnake
        group snake
        subGroups view=Snake orgs={genome}

'''


consensus_template = '''track consensus_{genome}
shortLabel CAT Annotation
longLabel CAT Annotation
description CAT Annotation
type bigGenePred
group cat_tracks
itemRgb on
visibility pack
searchIndex name,name2,sourceGene,sourceTranscript,geneName,geneName2
bigDataUrl {path}

'''


bgp_template = '''track {name}
shortLabel {label}
longLabel {label}
description {label}
type bigGenePred
group cat_tracks
itemRgb on
visibility pack
searchIndex name,name2,geneId,geneName
bigDataUrl {path}

'''


bigpsl_template = '''track {name}
shortLabel {short_label}
longLabel {long_label}
bigDataUrl {path}
type bigPsl
group cat_tracks
itemRgb on
visibility {visibility}
baseColorUseSequence lfExtra
indelDoubleInsert on
indelQueryInsert on
showDiffBasesAllScales .
showDiffBasesMaxZoom 10000.0
showCdsAllScales .
showCdsMaxZoom 10000.0
searchIndex name

'''


denovo_template = '''track {name}
shortlabel {short_label}
longLabel {long_label}
description {description}
bigDataUrl {path}
type bigGenePred
group cat_tracks
itemRgb on
searchIndex geneName,assignedGeneId,name,name2

'''


bam_composite_template = '''track bams_{genome}
group cat_tracks
compositeTrack on
shortLabel IsoSeq
longLabel IsoSeq BAMs
dragAndDrop subTracks
visibility hide
type bam
indelDoubleInsert on
indelQueryInsert on
showNames off
pairEndsByName on

'''

bam_template = '''    track {bam}_{genome}
    parent bams_{genome}
    bigDataUrl {bam}
    shortLabel {bam}
    longLabel {bam}

'''


wiggle_template = '''track {mode}_{genome}
shortLabel {mode} expression
longLabel {mode} expression
type bigWig
group cat_tracks
bigDataUrl {path}
color 153,38,0
visibility hide

'''


splice_template = '''track splices_{genome}
type bigBed 12
group cat_tracks
shortLabel RNA-seq Splices
longLabel RNA-seq Splice Junctions
bigDataUrl {path}
visibility dense
color 45,125,204

'''


error_template = '''track error_{genome}
type bigBed 12
group cat_tracks
shortLabel Consensus Indels
longLabel Consensus Indels
bigDataUrl {path}
visibility hide

'''
