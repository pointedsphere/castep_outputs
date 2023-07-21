#!/usr/bin/env python

"""
Extract results from .castep file for comparison and use
by testcode.pl.

Port of extract_results.pl
"""

from collections import defaultdict
import io
import re

from .utility import (EXPNUMBER_RE, FNUMBER_RE, INTNUMBER_RE, SHELL_RE,
                      ATREG, SPECIES_RE, ATDAT3VEC, SHELLS,
                      labelled_floats, fix_data_types, add_aliases, to_type,
                      stack_dict, get_block, get_numbers, normalise_string)
from .parse_extra_files import (parse_bands_file, parse_hug_file, parse_phonon_dos_file,
                                parse_efield_file, parse_xrd_sf_file, parse_elf_fmt_file,
                                parse_chdiff_fmt_file, parse_pot_fmt_file, parse_den_fmt_file)


def process_qdata(qdata):
    """ Special parse for phonon qdata """
    qdata = {key: val
             for key, val in qdata.items()
             if any(val) or key == "qpt"}
    fix_data_types(qdata,
                   {"qpt": float,
                    "N": int,
                    "frequency": float,
                    "intensity": float,
                    "raman_intensity": float
                    })
    return qdata


def parse_magres_block(task, inp):
    """ Parse MagRes data tables from inp according to task """

    data = defaultdict(list)
    data["task"] = task
    curr_re = MAGRES_RE[task]
    for line in inp:
        if match := curr_re.match(line):
            stack_dict(data, match.groupdict())

    if data:
        fix_data_types(data, {"index": int,
                              "iso": float,
                              "aniso": float,
                              "cq": float,
                              "eta": float})
        if "asym" in data:
            data["asym"] = [float(dat) if dat != "N/A" else None for dat in data["asym"]]

    return data


# Forces block
FORCES_BLOCK_RE = re.compile(r" ([a-zA-Z ]*)[Ff]orces \*+$")

# Regexp to identify phonon block in .castep file
CASTEP_PHONON_RE = re.compile(
    rf"""
    \s+\+\s+
    q-pt=\s*{INTNUMBER_RE}\s+
    \({labelled_floats(("qpt",), counts=(3,))}\)
    \s+
    ({FNUMBER_RE})\s+\+
    """, re.VERBOSE)

PROCESS_PHONON_RE = re.compile(
    rf"""\s+\+\s+
    (?P<N>\d+)\s+
    (?P<frequency>{FNUMBER_RE})\s*
    (?P<irrep>[a-zA-V])?\s*
    (?P<intensity>{FNUMBER_RE})?\s*
    (?P<active>[YN])?\s*
    (?P<raman_intensity>{FNUMBER_RE})?\s*
    (?P<raman_active>[YN])?\s*\+""", re.VERBOSE)

TDDFT_RE = re.compile(
    rf"""\s*\+\s*
    (?P<state>{INTNUMBER_RE})
    {labelled_floats(("energy", "error"))}
    \s*(?P<read>\w+)
    \s*\+TDDFT""", re.VERBOSE)

BS_RE = re.compile(
    rf"""
    Spin=\s*(?P<spin>{INTNUMBER_RE})\s*
    kpt=\s*{INTNUMBER_RE}\s*
    \({labelled_floats(("kx","ky","kz"))}\)\s*
    kpt-group=\s*(?P<kpgrp>{INTNUMBER_RE})
    """, re.VERBOSE)

THERMODYNAMICS_DATA_RE = re.compile(labelled_floats(("T", "E", "F", "S", "Cv")))

# Regexp to identify Mulliken ppoulation analysis line
CASTEP_POPN_RE = re.compile(rf"""\s*{ATREG}\s*(?P<spin_sep>up:)?
                            {labelled_floats((*SHELLS, "total", "charge", "spin"))}
                            ?"""   # Spin is optional
                            )

CASTEP_POPN_RE_DN = re.compile(r"\s+\d+\s*dn:" +
                               labelled_floats((*SHELLS, "total"))
                               )

# Regexp for born charges
BORN_RE = re.compile(rf"\s+{ATREG}(?P<charges>(?:\s*{FNUMBER_RE}){{3}})")

# MagRes REs
MAGRES_RE = [
    # "Chemical Shielding Tensor" 0
    re.compile(rf"\s*\|\s*{ATREG}{labelled_floats(('iso','aniso'))}\s*"
               rf"(?P<asym>{FNUMBER_RE}|N/A)\s*\|\s*"),
    # "Chemical Shielding and Electric Field Gradient Tensor" 1
    re.compile(rf"\s*\|\s*{ATREG}{labelled_floats(('iso','aniso'))}\s*"
               rf"(?P<asym>{FNUMBER_RE}|N/A)"
               rf"{labelled_floats(('cq', 'eta'))}\s*\|\s*"),
    # "Electric Field Gradient Tensor" 2
    re.compile(rf"\s*\|\s*{ATREG}{labelled_floats(('cq',))}\s*"
               rf"(?P<asym>{FNUMBER_RE}|N/A)\s*\|\s*"),
    # "(?:I|Ani)sotropic J-coupling" 3
    re.compile(rf"\s*\|\s*{ATREG}{labelled_floats(('iso','aniso'))}\s*"
               rf"(?P<asym>{FNUMBER_RE}|N/A){labelled_floats(('cq', 'eta'))}\s*\|\s*"),
    # "Hyperfine Tensor" 4
    re.compile(rf"\s*\|\s*{ATREG}{labelled_floats(('iso',))}\s*\|\s*")
    ]


def parse_castep_file(castep_file, verbose=False):
    """ Parse castep file into lists of dicts ready to JSONise """
    runs = []
    curr_run = {}

    for line in castep_file:
        if re.search(r"Run started", line):
            if curr_run:
                runs.append(curr_run)
            curr_run = defaultdict(list)
            curr_run["species_properties"] = defaultdict(dict)
            if verbose:
                print(f"Found run {len(runs) + 1}")

        elif block := get_block(line, castep_file,
                                r"^\s*\*+ .* Parameters \*+$",
                                r"^\s*\*+$"):
            if verbose:
                print("Found options")

            opt = {}
            curr_opt = {}
            curr_group = ""
            for line in block.splitlines():
                if match := re.match(r"\s*\*+ ([A-Za-z ]+) Parameters \*+", line):
                    if curr_opt:
                        opt[curr_group] = curr_opt
                    curr_group = match.group(1)
                    curr_opt = {}
                elif len(match := line.split(":")) > 1:
                    *key, val = map(normalise_string, match)
                    curr_opt[" ".join(key).strip()] = val.strip()

            if opt:
                curr_run["options"] = opt

        # Build Info
        elif block := get_block(line, castep_file,
                                r"^\s*Compiled for",
                                r"^\s*$"):

            if verbose:
                print("Found build info")

            curr = {}
            block = block.splitlines()

            curr['summary'] = " ".join(map(normalise_string, block[0:2]))
            for line in block[2:]:
                if ':' in line:
                    key, val = map(normalise_string, line.split(':', 1))
                    curr[key.strip()] = val.strip()

            if curr:
                curr_run["build_info"] = curr

        # Pseudo-atomic energy
        elif match := re.match(
                rf"\s*Pseudo atomic calculation performed for ({SPECIES_RE})(\s*{SHELL_RE})+",
                line):
            if verbose:
                print("Found pseudo-atomic energy")
            spec = match.group(1)
            castep_file.readline()
            line = castep_file.readline()
            energy = get_numbers(line)[1]
            curr_run["species_properties"][spec]["pseudo_atomic_energy"] = float(energy)

        # Mass
        elif block := get_block(line, castep_file, r"Mass of species in AMU", r"^ *$"):

            for line in block.splitlines():
                if (words := line.split()) and re.match(rf"{SPECIES_RE}\b", words[0]):
                    spec, mass = words
                    curr_run["species_properties"][spec]["mass"] = float(mass)

        # Electric Quadrupole Moment
        elif block := get_block(line, castep_file, r"Electric Quadrupole Moment", r"^ *$"):

            for line in block.splitlines():
                if (words := line.split()) and re.match(rf"{SPECIES_RE}\b", words[0]):
                    spec, quad = words[0:2]
                    curr_run["species_properties"][spec]["elec_quad"] = float(quad)

        # Pseudopots
        elif block := get_block(line, castep_file, r"Files used for pseudopotentials", r"^ *$"):

            for line in block.splitlines():
                if (words := line.split()) and re.match(rf"{SPECIES_RE}\b", words[0]):
                    spec, pspot = words
                    curr_run["species_properties"][spec]["pseudopot"] = pspot

        # Energies
        elif any((line.startswith("Final energy, E"),
                  line.startswith("Final energy"),
                  "Total energy corrected for finite basis set" in line,
                  re.search("(BFGS|TPSD): finished iteration.*with enthalpy", line))):
            if verbose:
                print("Found energy")
            curr_run["energies"].append(to_type(get_numbers(line)[-1], float))

        # Free energies
        elif re.match(rf"Final free energy \(E-TS\) += +({EXPNUMBER_RE})", line):
            if verbose:
                print("Found free energy (E-TS)")
            curr_run["free_energies"].append(to_type(get_numbers(line)[-1], float))

        # Solvation energy
        elif line.startswith(" Free energy of solvation"):
            if verbose:
                print("Found solvation energy")
            curr_run["solvation_energies"].append(*to_type(get_numbers(line), float))

        # Spin densities
        elif match := re.search(rf"Integrated Spin Density\s+=\s+({EXPNUMBER_RE})", line):
            if verbose:
                print("Found spin")
            curr_run["spin"].append(to_type(match.group(1), float))

        elif match := re.search(rf"Integrated \|Spin Density\|\s+=\s+({EXPNUMBER_RE})", line):
            if verbose:
                print("Found |spin|")
            curr_run["modspin"].append(to_type(match.group(1), float))

        # Finite basis correction parameter
        elif match := re.search(rf"finite basis dEtot\/dlog\(Ecut\) = +({FNUMBER_RE})", line):
            if verbose:
                print("Found dE/dlog(E)")
            curr_run["dedlne"].append(to_type(match.group(1), float))

        # Forces blocks
        elif block := get_block(line, castep_file, FORCES_BLOCK_RE.pattern, r"^ \*+$"):
            if "forces" not in curr_run:
                curr_run["forces"] = defaultdict(list)
            ftype = (ft_guess if (ft_guess := FORCES_BLOCK_RE.search(line).group(1))
                     else "non-descript")
            if verbose:
                print(f"Found {ftype} forces")

            accum = {match.group("spec", "index"): to_type(match.group("x", "y", "z"), float)
                     for line in block.splitlines()
                     if (match := ATDAT3VEC.search(line))}
            curr_run["forces"][ftype].append(accum)

        # Stress tensor block
        elif block := get_block(line, castep_file, r" Stress Tensor \*{11}", r"^ \*+$"):
            if verbose:
                print("Found Stress")

            accum = []
            for line in block.splitlines():
                numbers = get_numbers(line)
                if "*  x" in line:
                    accum += numbers[0:]
                elif "*  y" in line:
                    accum += numbers[1:]
                elif "*  z" in line:
                    accum += numbers[2:]
            curr_run["stress"] = to_type(accum, float)

        # Phonon block
        elif match := CASTEP_PHONON_RE.match(line):
            if verbose:
                print("Found phonon")

            qdata = defaultdict(list)
            qdata["qpt"] = match.group("qpt").split()
            if verbose:
                print(f"Reading qpt {' '.join(qdata['qpt'])}")

            while line := castep_file.readline():
                if match := CASTEP_PHONON_RE.match(line):
                    if qdata["qpt"] and qdata["qpt"] not in (phonon["qpt"]
                                                             for phonon in curr_run["phonons"]):
                        curr_run["phonons"].append(process_qdata(qdata))
                    qdata = defaultdict(list)
                    qdata["qpt"] = match.group("qpt").split()
                    if verbose:
                        print(f"Reading qpt {' '.join(qdata['qpt'])}")

                elif (re.match(r"\s+\+\s+Effective cut-off =", line) or
                      re.match(rf"\s+\+\s+q->0 along \((\s*{FNUMBER_RE}){{3}}\)\s+\+", line) or
                      re.match(r"\s+\+ -+ \+", line)):
                    continue
                elif match := PROCESS_PHONON_RE.match(line):

                    # ==By mode
                    # qdata["modes"].append(match.groupdict())
                    # ==By prop
                    stack_dict(qdata, match.groupdict())

                elif re.match(r"\s+\+\s+.*\+", line):
                    continue
                else:
                    break

            else:
                raise IOError(f"Unexpected end of file in {seedname}")

            if qdata["qpt"] and qdata["qpt"] not in (phonon["qpt"]
                                                     for phonon in curr_run["phonons"]):
                curr_run["phonons"].append(process_qdata(qdata))

            if verbose:
                print(f"Found {len(curr_run['phonons'])} phonon samples")

        # Raman tensors
        elif block := get_block(line, castep_file,
                                r"^ \+\s+Raman Susceptibility Tensors", r"^\s+$"):
            if verbose:
                print("Found Raman")

            modes = []
            curr_mode = {}
            for line in block.splitlines()[1:]:
                if "Mode number" in line:
                    if curr_mode:
                        modes.append(curr_mode)
                    curr_mode = {"tensor": [], "depolarisation": None}
                elif numbers := get_numbers(line):
                    curr_mode["tensor"].append(to_type(numbers[0:3], float))
                    if len(numbers) == 4:
                        curr_mode["depolarisation"] = to_type(numbers[3], float)

                elif re.search(r"^ \+\s+\+", line):  # End of 3x3+depol block
                    # Compute Invariants Tr(A) and Tr(A)^2-Tr(A^2) of Raman Tensor
                    tensor = curr_mode["tensor"]
                    curr_mode["trace"] = sum(tensor[i][i] for i in range(3))
                    curr_mode["II"] = (tensor[0][0]*tensor[1][1] +
                                       tensor[0][0]*tensor[2][2] +
                                       tensor[1][1]*tensor[2][2] -
                                       tensor[0][1]*tensor[1][0] -
                                       tensor[0][2]*tensor[2][0] -
                                       tensor[1][2]*tensor[2][1])
            if curr_mode:
                modes.append(curr_mode)
            curr_run["raman"].append(modes)

        # Born charges
        elif block := get_block(line, castep_file, r"^\s*Born Effective Charges\s*$", r"^ =+$"):
            if verbose:
                print("Found Born")

            lines = block.splitlines()

            born_accum = {}

            i = 0
            while i < len(lines):
                if match := BORN_RE.match(lines[i]):
                    born_accum[(match.group("spec"), match.group("index"))] = [to_type(match.group("charges").split(), float),
                                                                               to_type(lines[i+1].split(), float),
                                                                               to_type(lines[i+2].split(), float)]
                    curr_run["born"].append(born_accum)
                    i += 3
                else:
                    i += 1

        # Permittivity and NLO Susceptibility
        elif block := get_block(line, castep_file, r"^ +Optical Permittivity", r"^ =+$"):
            if verbose:
                print("Found optical permittivity")

            for line in block.splitlines():
                if re.match(rf"(?:\s*{FNUMBER_RE}){{3}}$", line):
                    curr_run["permittivity"].append(to_type(line.split(), float))

        # Polarisability
        elif block := get_block(line, castep_file, r"^ +Polarisabilit(y|ies)", r"^ =+$"):
            if verbose:
                print("Found polarisability")

            for line in block.splitlines():
                if re.match(rf"(?:\s*{FNUMBER_RE}){{3}}$", line):
                    curr_run["polarisability"].append(to_type(line.split(), float))

        # Non-linear
        elif block := get_block(line, castep_file,
                                r"^ +Nonlinear Optical Susceptibility", r"^ =+$"):
            if verbose:
                print("Found NLO")

            for line in block.splitlines():
                if re.match(rf"(?:\s*{FNUMBER_RE}){{6}}$", line):
                    curr_run["nlo"].append(line.split())

        # Thermodynamics
        elif block := get_block(line, castep_file,
                                r"\s*Thermodynamics\s*$",
                                r"\s+-+\s*$", cnt=3):
            if verbose:
                print("Found thermodynamics")

            accum = defaultdict(list)
            for line in block.splitlines():
                if match := THERMODYNAMICS_DATA_RE.match(line):
                    stack_dict(accum, match.groupdict())
                # elif re.match(r"\s+T\(", line):  # Can make dict/re based on labels
                #     thermo_label = line.split()

            fix_data_types(accum, {key: float for
                                   key in ("T", "E", "F", "S", "Cv")})

            curr_run["thermodynamics"].append(accum)

        # Mulliken Population Analysis
        elif block := get_block(line, castep_file,
                                r"Species\s+Ion\s+(Spin)?\s+s\s+p\s+d\s+f",
                                r"=+$", cnt=2):
            if verbose:
                print("Found Mulliken")

            for line in block.splitlines():
                if match := CASTEP_POPN_RE.match(line):
                    mull = match.groupdict()
                    if match.group("spin_sep"):  # We have spin separation
                        add_aliases(mull,
                                    {orb: f"up_{orb}" for orb in (*SHELLS, "total")},
                                    replace=True)
                        line = castep_file.readline()
                        match = CASTEP_POPN_RE_DN.match(line)
                        mull.update(match.groupdict())
                        mull["total"] = float(mull["up_total"]) + float(mull["dn_total"])
                    fix_data_types(mull, {"index": int,
                                          **{f"{orb}": float for orb in (*SHELLS, "total",
                                                                         "charge", "spin")},
                                          **{f"up_{orb}": float for orb in (*SHELLS, "total")},
                                          **{f"dn_{orb}": float for orb in (*SHELLS, "total")}})
                    curr_run["mull_popn"].append(mull)

        # Hirshfeld Population Analysis
        elif block := get_block(line, castep_file,
                                r"Species\s+Ion\s+Hirshfeld Charge \(e\)",
                                r"=+$", cnt=2):
            if verbose:
                print("Found Hirshfeld")

            for line in block.splitlines():
                if match := re.match(rf"\s+HIRSHFELD\s+\d+\s+({FNUMBER_RE})", line):
                    curr_run["hirshfeld"].append(to_type(match.group(1), float))

        # ELF
        elif block := get_block(line, castep_file,
                                r"ELF grid sample",
                                r"^-+$", cnt=2):
            if verbose:
                print("Found ELF")

            for line in block.splitlines():
                if match := re.match(rf"\s+ELF\s+\d+\s+({FNUMBER_RE})", line):
                    curr_run["elf"].append(to_type(match.group(1), float))

        # MD Block
        elif block := get_block(line, castep_file,
                                r"MD Data:",
                                r"^\s*x+\s*$"):

            if verbose:
                print(f"Found MD Block (step {len(curr_run['md'])+1})")

            curr_data = {match.group("key").strip(): float(match.group("val"))
                         for line in block.splitlines()
                         if (match := re.search(r"x\s+"
                                                r"(?P<key>[a-zA-Z][A-Za-z ]+):\s*"
                                                rf"(?P<val>{FNUMBER_RE})", line))}
            curr_run["md"].append(curr_data)

        # GeomOpt
        elif block := get_block(line, castep_file,
                                "Final Configuration",
                                r"^\s+x+$", cnt=2):

            if verbose:
                print("Found final geom configuration")

            accum = {match.group("spec", "index"): to_type(match.group("x", "y", "z"), float)
                     for line in block.splitlines()
                     if (match := ATDAT3VEC.search(line))}
            curr_run["final_configuration"].append(accum)

        # TDDFT
        elif block := get_block(line, castep_file,
                                "TDDFT excitation energies",
                                r"^\s*\+\s*=+\s*\+\s*TDDFT", cnt=2):

            if verbose:
                print("Found TDDFT excitations")

            tddata = defaultdict(list)
            for line in block.splitlines():
                if match := TDDFT_RE.match(line):

                    stack_dict(tddata, match.groupdict())

            if tddata:
                fix_data_types(tddata, {"state": int, "energy": float, "error": float})
                curr_run["tddft"].append(tddata)

        # Old band structure
        elif block := get_block(line, castep_file,
                                r"^\s+\+\s+B A N D",
                                r"^\s+=+$"):
            if verbose:
                print("Found old-style band-structure")

            qdata = defaultdict(list)

            for line in block.splitlines():
                if match := BS_RE.search(line):
                    if qdata:
                        fix_data_types(qdata, {"spin": int,
                                               "kx": float,
                                               "ky": float,
                                               "kz": float,
                                               "kpgrp": int,
                                               "band": int,
                                               "energy": float})
                        curr_run["bs"].append(qdata)
                    qdata = defaultdict(list)
                    qdata.update(match.groupdict())

                elif match := re.search(labelled_floats(("band", "energy"), sep=r"\s+"), line):
                    stack_dict(qdata, match.groupdict())

            if qdata:
                fix_data_types(qdata, {"spin": int,
                                       "kx": float,
                                       "ky": float,
                                       "kz": float,
                                       "kpgrp": int,
                                       "band": int,
                                       "energy": float})
                curr_run["bs"].append(qdata)

        # Chemical shielding
        elif block := get_block(line, castep_file,
                                r"Chemical Shielding Tensor",
                                r"=+$"):
            if verbose:
                print("Found Chemical Shielding Tensor")

            data = parse_magres_block(0, block.splitlines())
            curr_run["chem_shield"].append(data)

        elif block := get_block(line, castep_file,
                                r"Chemical Shielding and Electric Field Gradient Tensor",
                                r"=+$"):
            if verbose:
                print("Found Chemical Shielding + EField Tensor")

            data = parse_magres_block(1, block.splitlines())
            curr_run["chem_shield"].append(data)

        elif block := get_block(line, castep_file,
                                r"Electric Field Gradient Tensor",
                                r"=+$"):

            if verbose:
                print("Found EField Tensor")

            data = parse_magres_block(2, block.splitlines())
            curr_run["chem_shield"].append(data)

        # TODO: Check this is valid
        elif block := get_block(line, castep_file,
                                r"(?:I|Ani)sotropic J-coupling",
                                r"=+$"):
            if verbose:
                print("Found J-coupling")

            data = parse_magres_block(3, block.splitlines())
            curr_run["chem_shield"].append(data)

        elif block := get_block(line, castep_file,
                                r"\|\s*Hyperfine Tensor\s*\|",
                                r"=+$"):

            if verbose:
                print("Found Hyperfine tensor")

            data = parse_magres_block(4, block.splitlines())
            curr_run["chem_shield"].append(data)

        # --- Extra blocks for testing

        # Hugoniot data
        elif block := get_block(line, castep_file,
                                r"BEGIN hug",
                                r"END hug"):
            if verbose:
                print("Found hug block")

            block = io.StringIO(block)
            data = parse_hug_file(block)
            curr_run["hug"].append(data)

        # Bands block (spectral data)
        elif block := get_block(line, castep_file,
                                r"BEGIN bands",
                                r"END bands"):
            if verbose:
                print("Found bands block")

            block = io.StringIO(block)
            data = parse_bands_file(block)
            curr_run["bands"].append(data["bands"])

        elif block := get_block(line, castep_file,
                                r"BEGIN phonon_dos",
                                r"END phonon_dos"):

            if verbose:
                print("Found phonon_dos block")

            block = io.StringIO(block)
            data = parse_phonon_dos_file(block)
            curr_run["phonon_dos"] = data["dos"]
            curr_run["gradients"] = data["gradients"]

        # E-Field
        elif block := get_block(line, castep_file,
                                r"BEGIN efield",
                                r"END efield"):
            if verbose:
                print("Found efield block")

            block = io.StringIO(block)
            data = parse_efield_file(block)
            curr_run["oscillator_strengths"] = data["oscillator_strengths"]
            curr_run["permittivity"] = data["permittivity"]

        # XRD Structure Factor
        elif block := get_block(line, castep_file,
                                r"BEGIN xrd_sf",
                                r"END xrd_sf"):

            if verbose:
                print("Found xrdsf")

            block = "\n".join(block.splitlines()[1:-1])  # Strip begin/end tags lazily
            block = io.StringIO(block)
            data = parse_xrd_sf_file(block)

            curr_run["xrd_sf"] = data

        # ELF FMT
        elif block := get_block(line, castep_file,
                                r"BEGIN elf_fmt",
                                r"END elf_fmt"):

            if verbose:
                print("Found ELF fmt")

            block = "\n".join(block.splitlines()[1:-1])  # Strip begin/end tags lazily
            block = io.StringIO(block)
            data = parse_elf_fmt_file(block)
            if "kpt-data" not in curr_run:
                curr_run["kpt-data"] = data
            else:
                curr_run["kpt-data"].update(data)

        # CHDIFF FMT
        elif block := get_block(line, castep_file,
                                r"BEGIN chdiff_fmt",
                                r"END chdiff_fmt"):

            if verbose:
                print("Found CHDIFF fmt")

            block = "\n".join(block.splitlines()[1:-1])  # Strip begin/end tags lazily
            block = io.StringIO(block)
            data = parse_chdiff_fmt_file(block)
            if "kpt-data" not in curr_run:
                curr_run["kpt-data"] = data
            else:
                curr_run["kpt-data"].update(data)

        # POT FMT
        elif block := get_block(line, castep_file,
                                r"BEGIN pot_fmt",
                                r"END pot_fmt"):

            if verbose:
                print("Found POT fmt")

            block = "\n".join(block.splitlines()[1:-1])  # Strip begin/end tags lazily
            block = io.StringIO(block)
            data = parse_pot_fmt_file(block)
            if "kpt-data" not in curr_run:
                curr_run["kpt-data"] = data
            else:
                curr_run["kpt-data"].update(data)

        # DEN FMT
        elif block := get_block(line, castep_file,
                                r"BEGIN den_fmt",
                                r"END den_fmt"):

            if verbose:
                print("Found DEN fmt")

            block = "\n".join(block.splitlines()[1:-1])  # Strip begin/end tags lazily
            block = io.StringIO(block)
            data = parse_den_fmt_file(block)
            if "kpt-data" not in curr_run:
                curr_run["kpt-data"] = data
            else:
                curr_run["kpt-data"].update(data)

    if curr_run:
        fix_data_types(curr_run, {"energies": float,
                                  "solvation": float})
        runs.append(curr_run)
    return runs
