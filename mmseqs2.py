import jax
import requests
import hashlib
import tarfile
import time
import pickle
import os
import re

import random
import tqdm.notebook

import numpy as np

from string import ascii_uppercase,ascii_lowercase

alphabet_list = list(ascii_uppercase + ascii_lowercase)

aatypes = set('ACDEFGHIKLMNPQRSTVWY')


def rm(x):
    '''remove data from device'''
    jax.tree_util.tree_map(lambda y: y.device_buffer.delete(), x)


def to(x, device="cpu"):
    '''move data to device'''
    d = jax.devices(device)[0]
    return jax.tree_util.tree_map(lambda y: jax.device_put(y, d), x)


def clear_mem(device="gpu"):
    '''remove all data from device'''
    backend = jax.lib.xla_bridge.get_backend(device)
    for buf in backend.live_buffers(): buf.delete()

def get_hash(x):
  return hashlib.sha1(x.encode()).hexdigest()

def run_mmseqs2(x, prefix, use_env=True, use_filter=True,
                use_templates=False, filter=None, host_url="https://a3m.mmseqs.com"):
    def submit(seqs, mode, N=101):
        n, query = N, ""
        for seq in seqs:
            query += f">{n}\n{seq}\n"
            n += 1

        res = requests.post(f'{host_url}/ticket/msa', data={'q': query, 'mode': mode})
        try:
            out = res.json()
        except ValueError:
            out = {"status": "UNKNOWN"}
        return out

    def status(ID):
        res = requests.get(f'{host_url}/ticket/{ID}')
        try:
            out = res.json()
        except ValueError:
            out = {"status": "UNKNOWN"}
        return out

    def download(ID, path):
        res = requests.get(f'{host_url}/result/download/{ID}')
        with open(path, "wb") as out: out.write(res.content)

    # process input x
    seqs = [x] if isinstance(x, str) else x

    # compatibility to old option
    if filter is not None:
        use_filter = filter

    # setup mode
    if use_filter:
        mode = "env" if use_env else "all"
    else:
        mode = "env-nofilter" if use_env else "nofilter"

    # define path
    path = f"{prefix}_{mode}"
    if not os.path.isdir(path): os.mkdir(path)

    # call mmseqs2 api
    tar_gz_file = f'{path}/out.tar.gz'
    N, REDO = 101, True

    # deduplicate and keep track of order
    seqs_unique = sorted(list(set(seqs)))
    Ms = [N + seqs_unique.index(seq) for seq in seqs]

    # lets do it!
    if not os.path.isfile(tar_gz_file):
        while REDO:
            # Resubmit job until it goes through
            out = submit(seqs_unique, mode, N)
            while out["status"] in ["UNKNOWN", "RATELIMIT"]:
                # resubmit
                time.sleep(5 + random.randint(0, 5))
                out = submit(seqs_unique, mode, N)

            # wait for job to finish
            ID, TIME = out["id"], 0
            while out["status"] in ["UNKNOWN", "RUNNING", "PENDING"]:
                t = 5 + random.randint(0, 5)
                time.sleep(t)
                out = status(ID)
                if out["status"] == "RUNNING":
                    TIME += t
                # if TIME > 900 and out["status"] != "COMPLETE":
                #  # something failed on the server side, need to resubmit
                #  N += 1
                #  break

            if out["status"] == "COMPLETE":
                REDO = False

            if out["status"] == "ERROR":
                REDO = False
                raise Exception(f'MMseqs2 API is giving errors. Please confirm your input is a valid protein sequence. If error persists, please try again an hour later.')

        # Download results
        download(ID, tar_gz_file)

    # prep list of a3m files
    a3m_files = [f"{path}/uniref.a3m"]
    if use_env: a3m_files.append(f"{path}/bfd.mgnify30.metaeuk30.smag30.a3m")

    # extract a3m files
    if not os.path.isfile(a3m_files[0]):
        with tarfile.open(tar_gz_file) as tar_gz:
            def is_within_directory(directory, target):
                
                abs_directory = os.path.abspath(directory)
                abs_target = os.path.abspath(target)
            
                prefix = os.path.commonprefix([abs_directory, abs_target])
                
                return prefix == abs_directory
            
            def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
            
                for member in tar.getmembers():
                    member_path = os.path.join(path, member.name)
                    if not is_within_directory(path, member_path):
                        raise Exception("Attempted Path Traversal in Tar File")
            
                tar.extractall(path, members, numeric_owner=numeric_owner) 
                
            
            safe_extract(tar_gz, path)

            # templates
    if use_templates:
        templates = {}
        print("seq\tpdb\tcid\tevalue")
        for line in open(f"{path}/pdb70.m8", "r"):
            p = line.rstrip().split()
            M, pdb, qid, e_value = p[0], p[1], p[2], p[10]
            M = int(M)
            if M not in templates: templates[M] = []
            templates[M].append(pdb)
            if len(templates[M]) <= 20:
                print(f"{int(M) - N}\t{pdb}\t{qid}\t{e_value}")

        template_paths = {}
        for k, TMPL in templates.items():
            TMPL_PATH = f"{prefix}_{mode}/templates_{k}"
            if not os.path.isdir(TMPL_PATH):
                os.mkdir(TMPL_PATH)
                TMPL_LINE = ",".join(TMPL[:20])
                os.system(f"curl -s https://a3m-templates.mmseqs.com/template/{TMPL_LINE} | tar xzf - -C {TMPL_PATH}/")
                os.system(f"cp {TMPL_PATH}/pdb70_a3m.ffindex {TMPL_PATH}/pdb70_cs219.ffindex")
                os.system(f"touch {TMPL_PATH}/pdb70_cs219.ffdata")
            template_paths[k] = TMPL_PATH

    # gather a3m lines
    a3m_lines = {}
    for a3m_file in a3m_files:
        update_M, M = True, None
        for line in open(a3m_file, "r"):
            if len(line) > 0:
                if "\x00" in line:
                    line = line.replace("\x00", "")
                    update_M = True
                if line.startswith(">") and update_M:
                    M = int(line[1:].rstrip())
                    update_M = False
                    if M not in a3m_lines: a3m_lines[M] = []
                a3m_lines[M].append(line)

    # return results
    a3m_lines = ["".join(a3m_lines[n]) for n in Ms]

    if use_templates:
        template_paths_ = []
        for n in Ms:
            if n not in template_paths:
                template_paths_.append(None)
                print(f"{n - N}\tno_templates_found")
            else:
                template_paths_.append(template_paths[n])
        template_paths = template_paths_

    if isinstance(x, str):
        return (a3m_lines[0], template_paths[0]) if use_templates else a3m_lines[0]
    else:
        return (a3m_lines, template_paths) if use_templates else a3m_lines


