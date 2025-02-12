import os
import os.path as osp
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Lock
from typing import Any, Dict, List, Tuple

import mmengine
import numpy as np
from mmengine.config import ConfigDict
from tqdm import tqdm

from opencompass.registry import RUNNERS, TASKS
from opencompass.utils import get_logger

from .base import BaseRunner


@RUNNERS.register_module()
class LocalRunner(BaseRunner):
    """Local runner. Start tasks by local python.

    Args:
        task (ConfigDict): Task type config.
        max_num_workers (int): Max number of workers to run in parallel.
            Defaults to 16.
        max_workers_per_gpu (int): Max number of workers to run for one GPU.
            Defaults to 1.
        debug (bool): Whether to run in debug mode.
        lark_bot_url (str): Lark bot url.
    """

    def __init__(self,
                 task: ConfigDict,
                 max_num_workers: int = 16,
                 debug: bool = False,
                 max_workers_per_gpu: int = 1,
                 lark_bot_url: str = None):
        super().__init__(task=task, debug=debug, lark_bot_url=lark_bot_url)
        self.max_num_workers = max_num_workers
        self.max_workers_per_gpu = max_workers_per_gpu

    def launch(self, tasks: List[Dict[str, Any]]) -> List[Tuple[str, int]]:
        """Launch multiple tasks.

        Args:
            tasks (list[dict]): A list of task configs, usually generated by
                Partitioner.

        Returns:
            list[tuple[str, int]]: A list of (task name, exit code).
        """

        status = []
        if self.debug:
            for task in tasks:
                task = TASKS.build(dict(type=self.task_cfg.type, cfg=task))
                task_name = task.name
                # get cmd
                mmengine.mkdir_or_exist('tmp/')
                param_file = f'tmp/{os.getpid()}_params.py'
                task.cfg.dump(param_file)
                cmd = task.get_command(cfg_path=param_file,
                                       template='{task_cmd}')
                # run in subprocess if starts with torchrun etc.
                if cmd.startswith('python'):
                    task.run()
                else:
                    subprocess.run(cmd, shell=True, text=True)
                os.remove(param_file)
                status.append((task_name, 0))
        else:
            import torch
            if 'CUDA_VISIBLE_DEVICES' in os.environ:
                all_gpu_ids = [
                    int(i) for i in re.findall(
                        r'(?<!-)\d+', os.getenv('CUDA_VISIBLE_DEVICES'))
                ]
            else:
                all_gpu_ids = list(range(torch.cuda.device_count()))

            if len(all_gpu_ids) > 0:
                gpus = np.zeros(max(all_gpu_ids) + 1, dtype=np.uint)
                gpus[all_gpu_ids] = self.max_workers_per_gpu
            else:
                gpus = np.array([], dtype=np.uint)

            pbar = tqdm(total=len(tasks))
            lock = Lock()

            def submit(task, index):
                task = TASKS.build(dict(type=self.task_cfg.type, cfg=task))
                num_gpus = task.num_gpus
                assert len(gpus) >= num_gpus

                while True:
                    lock.acquire()
                    if sum(gpus > 0) >= num_gpus:
                        gpu_ids = np.where(gpus)[0][:num_gpus]
                        gpus[gpu_ids] -= 1
                        lock.release()
                        break
                    lock.release()
                    time.sleep(1)

                if num_gpus > 0:
                    tqdm.write(f'launch {task.name} on GPU ' +
                               ','.join(map(str, gpu_ids)))
                else:
                    tqdm.write(f'launch {task.name} on CPU ')

                res = self._launch(task, gpu_ids, index)
                pbar.update()

                with lock:
                    gpus[gpu_ids] += 1

                return res

            with ThreadPoolExecutor(
                    max_workers=self.max_num_workers) as executor:
                status = executor.map(submit, tasks, range(len(tasks)))

        return status

    def _launch(self, task, gpu_ids, index):
        """Launch a single task.

        Args:
            task (BaseTask): Task to launch.

        Returns:
            tuple[str, int]: Task name and exit code.
        """

        task_name = task.name

        # Dump task config to file
        mmengine.mkdir_or_exist('tmp/')
        param_file = f'tmp/{os.getpid()}_{index}_params.py'
        task.cfg.dump(param_file)

        # Build up slurm command
        tmpl = 'CUDA_VISIBLE_DEVICES=' + ','.join(str(i) for i in gpu_ids)
        tmpl += ' {task_cmd}'
        get_cmd = partial(task.get_command, cfg_path=param_file, template=tmpl)
        cmd = get_cmd()

        logger = get_logger()
        logger.debug(f'Running command: {cmd}')

        # Run command
        out_path = task.get_log_path(file_extension='out')
        mmengine.mkdir_or_exist(osp.split(out_path)[0])
        stdout = open(out_path, 'w', encoding='utf-8')

        result = subprocess.run(cmd,
                                shell=True,
                                text=True,
                                stdout=stdout,
                                stderr=stdout)

        if result.returncode != 0:
            logger.warning(f'task {task_name} fail, see\n{out_path}')

        # Clean up
        os.remove(param_file)
        return task_name, result.returncode
