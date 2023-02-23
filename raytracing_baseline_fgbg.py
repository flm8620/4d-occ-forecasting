import os
import json
import copy
import argparse
from datetime import datetime
import numpy as np

import torch
from torch import nn
from torch.utils.data import DataLoader
import struct
from chamferdist import ChamferDistance
from data.common import CollateFn, nuScenesVolume2Kitti
from utils.evaluation import compute_chamfer_distance, compute_chamfer_distance_inner, compute_ray_errors, clamp


def get_grid_mask(points, pc_range):
    points = points.T
    mask1 = np.logical_and(pc_range[0] <= points[0], points[0] <= pc_range[3])
    mask2 = np.logical_and(pc_range[1] <= points[1], points[1] <= pc_range[4])
    mask3 = np.logical_and(pc_range[2] <= points[2], points[2] <= pc_range[5])

    mask = mask1 & mask2 & mask3

    return mask

def get_clamped_gt(n_output, points, tindex, pc_range,
        eval_within_grid=False, eval_outside_grid=False, get_indices=False):
    pcds = []
    if get_indices:
        indices = []
    for t in range(n_output):
        mask = tindex == t
        if eval_within_grid:
            mask = np.logical_and(mask, get_grid_mask(points, pc_range))
        if eval_outside_grid:
            mask = np.logical_and(mask, ~get_grid_mask(points, pc_range))
        # skip the ones with no data
        if not mask.any():
            continue
        if get_indices:
            idx = np.arange(points.shape[0])
            indices.append(idx[mask])
        gt_pts = points[mask, :3]
        pcds.append(torch.from_numpy(gt_pts))
    if get_indices:
        return pcds, indices
    else:
        return pcds

def make_data_loader(cfg, args):
    dataset_kwargs={
        "pc_range": cfg["pc_range"],
        "voxel_size": cfg["voxel_size"],
        "n_input": cfg["n_input"],
        "input_step": cfg["input_step"],
        "n_output": cfg["n_output"],
        "output_step": cfg["output_step"],
    }
    data_loader_kwargs={
        "pin_memory": False,  # NOTE
        "shuffle": True,
        "batch_size": args.batch_size,
        "num_workers": args.num_workers,
    }

    if cfg["dataset"].lower() == "nuscenes":
        from data.nusc import nuScenesDataset
        from nuscenes.nuscenes import NuScenes
        from data.common import CollateFn

        if args.test_split == "test":
            cfg["nusc_version"] = "v1.0-test"
        nusc = NuScenes(cfg["nusc_version"], cfg["nusc_root"])

        Dataset = nuScenesDataset
        data_loader = DataLoader(
            Dataset(nusc, args.test_split, dataset_kwargs),
            collate_fn=CollateFn,
            **data_loader_kwargs,
        )
    else:
        raise NotImplementedError(f"Dataset {cfg['dataset']} is not supported.")

    return data_loader


def test(args):
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    #
    device_count = torch.cuda.device_count()
    print("Device count", device_count)
    if args.batch_size % device_count != 0:
        raise RuntimeError(
            f"Batch size ({args.batch_size}) cannot be divided by device count ({device_count})"
        )

    #
    with open(f"{args.config_path}", "r") as f:
        cfg=json.load(f)

    # dataset
    data_loader=make_data_loader(cfg, args)

    # instantiate a model and a renderer
    _n_input, _n_output=cfg["n_input"], cfg["n_output"]
    _pc_range, _voxel_size=cfg["pc_range"], cfg["voxel_size"]

    #
    dt=datetime.now()

    metrics = {
        "count": 0.0,
        "chamfer_distance": 0.0,
        "chamfer_distance_inner": 0.0,
        "l1_error": 0.0,
        "absrel_error": 0.0
    }
    for i, batch in enumerate(data_loader):

        filenames=batch[0]

        input_points, input_tindex=batch[1:3]
        output_origin, output_points, output_tindex=batch[3:6]  # removed output_labels as
                                                                # the last returned argument
        assert cfg["dataset"] == "nuscenes"
        output_labels = batch[6]

        assert output_points.shape[0] == 1
        static_lidar_points = output_labels < 0.5
        dynamic_lidar_points = output_labels > 0.5

        # iterate through the batch
        for j in range(output_points.shape[0]):  # iterate through the batch

            pred_pcd_agg = input_points[j].cpu()

            gt_pcds = get_clamped_gt(
                    _n_output,
                    output_points[j].cpu().numpy(),
                    output_tindex[j].cpu().numpy(),
                    _pc_range,
                    args.eval_within_grid,
                    args.eval_outside_grid
                )

            # load predictions
            for k in range(len(gt_pcds)):
                origin = output_origin[j][k].cpu().numpy()
                gt_pcd = gt_pcds[k]
                seq = filenames[j][1]
                pred_pcd = compute_ray_errors(
                        pred_pcd_agg,
                        gt_pcd,
                        torch.from_numpy(origin),
                        device,
                        return_interpolated_pcd=True)

                if args.fg_bg == "fg":
                    dynamic = dynamic_lidar_points[j][output_tindex[j] == k]
                    gt_pcd = gt_pcd[dynamic, :3]
                    pred_pcd = pred_pcd[dynamic, :3]
                else:
                    static = static_lidar_points[j][output_tindex[j] == k]
                    gt_pcd = gt_pcd[static, :3]
                    pred_pcd = pred_pcd[static, :3]

                if gt_pcd.shape[0] == 0:
                    continue

                # get the metrics
                metrics["count"] += 1
                metrics["chamfer_distance"] += compute_chamfer_distance(pred_pcd, gt_pcd, device)
                metrics["chamfer_distance_inner"] += compute_chamfer_distance_inner(pred_pcd, gt_pcd, device)
                l1_error, absrel_error = compute_ray_errors(pred_pcd, gt_pcd, torch.from_numpy(origin), device)
                metrics["l1_error"] += l1_error
                metrics["absrel_error"] += absrel_error

        print("Batch {"+str(i)+"/"+str(len(data_loader))+"}:", "Chamfer Distance:", metrics["chamfer_distance"] / metrics["count"])
        print("Batch {"+str(i)+"/"+str(len(data_loader))+"}:", "Chamfer Distance Inner:", metrics["chamfer_distance_inner"] / metrics["count"])
        print("Batch {"+str(i)+"/"+str(len(data_loader))+"}:", "L1 Error:", metrics["l1_error"] / metrics["count"])
        print("Batch {"+str(i)+"/"+str(len(data_loader))+"}:", "AbsRel Error:", metrics["absrel_error"] / metrics["count"])
        print("Batch {"+str(i)+"/"+str(len(data_loader))+"}:", "Count:", metrics["count"])

    print("Final Chamfer Distance:", metrics["chamfer_distance"] / metrics["count"])
    print("Final Chamfer Distance Inner:", metrics["chamfer_distance_inner"] / metrics["count"])
    print("Final L1 Error:", metrics["l1_error"] / metrics["count"])
    print("Final AbsRel Error:", metrics["absrel_error"] / metrics["count"])



if __name__ == "__main__":
    parser=argparse.ArgumentParser()

    parser.add_argument("--config-path", type=str, required=True)
    parser.add_argument("--test-split", type=str, required=True)
    parser.add_argument("--batch-size", type=int, default=36)
    parser.add_argument("--num-workers", type=int, default=4)
    parser.add_argument("--compute-chamfer-distance", action="store_true")
    parser.add_argument("--eval-within-grid", action="store_true")
    parser.add_argument("--eval-outside-grid", action="store_true")
    parser.add_argument("--plot-metrics", action="store_true")
    parser.add_argument("--fg-bg", default='fg', required=True, type=str)
    parser.add_argument("--write-dense-pointcloud", action="store_true")

    args=parser.parse_args()
    torch.random.manual_seed(0)
    np.random.seed(0)
    test(args)
