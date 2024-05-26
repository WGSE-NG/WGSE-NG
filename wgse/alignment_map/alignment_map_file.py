import logging
import pickle
import shlex
import subprocess
from pathlib import Path

from wgse.alignment_map.alignment_map_header import AlignmentMapHeader
from wgse.alignment_map.alignment_stats_calculator import AlignmentStatsCalculator
from wgse.alignment_map.index_stats_calculator import (
    IndexStatsCalculator,
    SequenceStatistics,
)
from wgse.configuration import MANAGER_CFG
from wgse.data.alignment_map.alignment_map_file_info import AlignmentMapFileInfo
from wgse.data.file_type import FileType
from wgse.data.gender import Gender
from wgse.data.mitochondrial_model_type import MitochondrialModelType
from wgse.data.sequence_type import SequenceType
from wgse.data.sorting import Sorting
from wgse.fasta.reference import Reference, ReferenceStatus
from wgse.reference_genome.repository_manager import RepositoryManager
from wgse.mtDNA.mt_dna import MtDNA
from wgse.utility.samtools import Samtools

logger = logging.getLogger(__name__)


class AlignmentMapFile:
    SUPPORTED_FILES = {
        ".bam": FileType.BAM,
        ".sam": FileType.SAM,
        ".cram": FileType.CRAM,
    }

    def __init__(
        self,
        path: Path,
        samtools=Samtools(),
        repository: RepositoryManager = RepositoryManager(),
        mtdna: MtDNA = MtDNA(),
        config=MANAGER_CFG.EXTERNAL,
    ) -> None:
        if isinstance(path, str):
            path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Unable to find file {path.name}")
        if path.suffix.lower() not in AlignmentMapFile.SUPPORTED_FILES:
            raise RuntimeError(f"Unrecognized file extension: {path.name}")

        self.path: Path = path
        self.meta_file: Path = self.path.with_suffix(".pickle")
        self._repo = repository
        self._samtools = samtools
        self._mtdna = mtdna
        self._config = config
        self.header = self._load_header()
        self.file_info = self._initialize_file_info()

    def subset(self, percentage):
        if percentage < 0.0 or percentage > 100.0:
            raise RuntimeError(
                f"Percentage needs to be between 0 and 100 to produce a subset but was {percentage}"
            )

        output = self.path.with_stem(f"{self.path.stem}_{percentage}_percent")
        percentage /= 100
        reference = None
        if self.file_info.file_type == FileType.CRAM:
            reference = self.file_info.reference_genome.ready_reference
            if reference is None:
                raise FileNotFoundError(
                    "Reference genome was not found but is mandatory for CRAM files"
                )

        self._samtools.view(
            self.path, output, subsample=percentage, reference=reference
        )
        self._samtools.index(output)

        if not output.exists():
            raise RuntimeError(f"Unable to find the subset file at {output}")
        return AlignmentMapFile(output)

    def _load_header(self) -> AlignmentMapHeader:
        return AlignmentMapHeader(self._samtools.header(self.path))

    def _to_fastq(self, io):
        pass

    def _to_fasta(self, regions="", io=None):
        input = self.path
        suffixes = self.path.suffixes.copy()
        suffixes[-1] = ".fasta"
        output = self.path.with_name(self.path.stem + "".join(suffixes))
        reference = str(self.file_info.reference_genome.ready_reference.fasta)
        faidx_opt = f'faidx "{reference}" {regions}'
        consensus_opt = f'consensus "{input}" -o "{output}"'
        # if io is not None:
        #     io = lambda r,w: io(r,w)

        faidx = self._samtools.fasta_index(faidx_opt, stdout=subprocess.PIPE)  # io=io)
        consensus = self._samtools.samtools(
            shlex.split(consensus_opt), stdin=faidx.stdout
        )
        consensus.communicate()
        return output

    def _to_alignment_map(self, target: FileType, region, io):
        suffixes = self.path.suffixes.copy()
        if len(suffixes) == 0:
            suffixes = [None]

        if target == FileType.BAM:
            suffixes[-1] = ".bam"
        elif target == FileType.CRAM:
            suffixes[-1] = ".cram"
            if self.file_info.reference_genome.ready_reference is None:
                raise RuntimeError(
                    "Reference genome was not found but is mandatory for CRAM files."
                )
            reference = self.file_info.reference_genome.ready_reference.fasta
        elif target == FileType.SAM:
            suffixes[-1] = ".sam"
        output = self.path.with_name(self.path.stem + "".join(suffixes))

        progress = None
        if io is not None:
            progress = lambda r, w: io(self.path.stat().st_size, r)  # NOQA
        return self._samtools.view(
            self.path, target, output, region, reference, io=progress
        )

    def convert(self, target: FileType, regions="", io=None):
        if self.file_info.file_type == target:
            raise ValueError("Target and source file type for conversion are identical")
        if target == FileType.FASTA:
            self._to_fasta(io=io)
        if target == FileType.FASTQ:
            self._to_fastq(io=io)
        return self._to_alignment_map(target, regions, io=io)

    def load_meta(self):
        try:
            if self.meta_file.exists():
                with self.meta_file.open("rb") as f:
                    return pickle.load(f)
        except Exception as e:
            logger.error(
                f"Error when loading meta-information for {self.meta_file.name}: {e!s}"
            )
        return None

    def save_meta(self, file_info=None):
        try:
            if file_info is None:
                file_info = self.file_info
            with self.meta_file.open("wb") as f:
                pickle.dump(file_info, f)
        except Exception as e:
            logger.error(
                f"Error when saving meta-information for {self.meta_file.name}: {e!s}"
            )

    def _initialize_file_info(self):
        meta = self.load_meta()
        if meta is not None:
            return meta
        file_info = AlignmentMapFileInfo()
        file_info.path = self.path
        file_info.file_type = AlignmentMapFile.SUPPORTED_FILES[self.path.suffix.lower()]
        file_info.sorted = self.header.metadata.sorted
        file_info.name_type_mtdna = self.header.mtdna_name_type()
        file_info.name_type_chromosomes = self.header.chromosome_name_type()
        file_info.sequence_count = self.header.sequence_count()
        file_info.indexed = self._indexed(file_info.file_type)
        file_info.gender = Gender.Unknown
        file_info.reference_genome = self._repo.find(
            list(self.header.sequences.values())
        )
        file_info.mitochondrial_dna_model = self.get_mitochondrial_dna_type(
            file_info.reference_genome
        )

        # Compute IndexStats automatically only if it's inexpensive to do so.
        # Otherwise, let the caller explicitly request them.
        inexpensive_index_stats = file_info.indexed and file_info.file_type not in [
            FileType.CRAM,
            FileType.SAM,
        ]

        if inexpensive_index_stats:
            indexed_stats = IndexStatsCalculator(self.path)
            file_info.index_stats = indexed_stats.get_stats()
            file_info.gender = self.get_gender(file_info.index_stats)

        # If file is not sorted computing AlignmentStats is expensive.
        # Let the caller request them.
        if file_info.sorted == Sorting.Coordinate:
            is_cram = file_info.file_type == FileType.CRAM
            has_reference = file_info.reference_genome.ready_reference is not None
            if is_cram and has_reference or not is_cram:
                calculator = AlignmentStatsCalculator(file_info)
                file_info.alignment_stats = calculator.get_stats()
        self.save_meta(file_info)
        return file_info

    def get_mitochondrial_dna_type(self, reference: Reference):
        mitochondrial_dna_model = MitochondrialModelType.Unknown
        if reference.status == ReferenceStatus.Available:
            matching = reference.matching[0]
            if matching.mitochondrial_model is not None:
                mitochondrial_dna_model = MitochondrialModelType[
                    matching.mitochondrial_model
                ]
        return mitochondrial_dna_model

    def _indexed(self, type=None):
        if type is None:
            type = self.file_info.file_type

        file = str(self.path)
        if type == FileType.BAM:
            return Path(file + ".bai").exists()
        elif type == FileType.CRAM:
            return Path(file + ".crai").exists()
        return False

    def get_gender(self, stats: list[SequenceStatistics]):
        x_stats = [x for x in stats if x.type == SequenceType.X]
        y_stats = [x for x in stats if x.type == SequenceType.Y]
        x_length = 0
        y_length = 0

        if len(x_stats) != 0 and len(x_stats) != 1:
            return Gender.Unknown

        if len(y_stats) != 0 and len(y_stats) != 1:
            return Gender.Unknown

        if len(x_stats) == 1:
            x_length = x_stats[0].mapped + x_stats[0].unmapped
        if len(y_stats) == 1:
            y_length = y_stats[0].mapped + y_stats[0].unmapped

        if x_length == 0 and y_length == 0:
            return Gender.Unknown
        elif y_length == 0 or (x_length / y_length) > 20:
            return Gender.Female
        else:
            return Gender.Male
