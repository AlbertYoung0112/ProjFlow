import sys
import os
import numpy as np
from pyproj import Proj, Transformer, CRS
import utm
import math
from geographiclib.geodesic import Geodesic
import geographiclib as gl
import multiprocessing as mp
import pandas as pd
import argparse
from tqdm import tqdm

AUSTIN_PROJ4_DESC = "+proj=lcc \
    +lat_1=31.8833333333333 +lat_2=30.1166666666667 +lat_0=29.6666666666667 \
    +lon_0=-100.333333333333 +x_0=2296583.333333 +y_0=9842500 \
    +datum=NAD83 +units=m +no_defs"

ALTITUDE_LB = 80
ALTITUDE_UB = 400
ERR_UB = 2000

parser = argparse.ArgumentParser(description="Preprocess the GPS-data")
parser.add_argument('inputDir', type=str, metavar="DirIn", help='Raw dataset Dir')
parser.add_argument('outputDir', type=str, metavar="DirOut", help='Output Dir')
parser.add_argument("--sizeX", type=int, metavar='SX', help='GeoHash Block Size X', default=ERR_UB)
parser.add_argument("--sizeY", type=int, metavar='SY', help='GeoHash Block Size Y', default=ERR_UB)
parser.add_argument("--sizeZ", type=int, metavar='SZ', help='GeoHash Block Size Z', default=ERR_UB)
parser.add_argument("--thread", type=int, help='Multiprocessing Thread Pool Size', default=4)
parser.add_argument("--mapLbX", type=float, default=-1)
parser.add_argument("--mapLbY", type=float, default=-1)
parser.add_argument("--mapLbZ", type=float, default=-1)

crsWGS84 = CRS.from_epsg(4326)
crsAustin = CRS.from_proj4(AUSTIN_PROJ4_DESC)
transformer = Transformer.from_crs(crsWGS84, crsAustin)

def applyTransform(wgs):
    return transformer.transform(wgs[0], wgs[1], wgs[2])

def preproc(filePath, fileName, outputPath, tqdmHandle, blockSize):
    stat = pd.DataFrame()   # Collect the statistic data

    # Load the dataset
    tqdmHandle.set_description("Loading Dataset")
    dataset = pd.read_csv(os.path.join(filePath, fileName))
    stat.loc[0, 'RawPings'] = dataset.shape[0]

    # Clean the accuracy - Drop pings with large acc error
    acc = dataset.loc[:, ('horizontal_accuracy', 'vertical_accuracy')].fillna(np.inf)
    err = np.linalg.norm(acc, ord=2, axis=1)
    errPings = np.logical_or(err == 0, err > ERR_UB)
    dataset.drop(dataset[errPings].index, inplace=True, axis=0)

    # Clean the altitude - Set the altitude to the average
    alt = dataset['altitude'].fillna(0)
    validAlt = np.logical_and(alt > ALTITUDE_LB, alt < ALTITUDE_UB)
    avgAlt = np.average(alt[validAlt])
    dataset.loc[~validAlt, 'altitude'] = avgAlt
    tqdmHandle.set_description(f"Fix {np.sum(~validAlt)} invalid altitudes.")

    # Sort the data according to the time
    dataset = dataset.sort_values(by='location_at')
    
    # Get the map boundary
    wgsMapLb = np.zeros(3)
    wgsMapLb[0] = np.min(dataset['latitude']) if args.mapLbX == -1 else args.mapLbX
    wgsMapLb[1] = np.min(dataset['longitude']) if args.mapLbY == -1 else args.mapLbY
    wgsMapLb[2] = np.min(dataset['altitude']) if args.mapLbZ == -1 else args.mapLbZ
    mapLb = np.array(transformer.transform(wgsMapLb[0], wgsMapLb[1], wgsMapLb[2]))

    # Convert to local cartesian coordinate
    tqdmHandle.set_description("Convert the CRS")
    pos = dataset.loc[:, ("latitude", "longitude", "altitude")]
    with mp.Pool(args.thread) as pool:
        localPos = pool.map(applyTransform, pos.values)
    localPos = np.array(localPos)

    # Get the geohash coordinate
    geohashBlk = np.around((localPos - mapLb) / blockSize).astype(np.int)

    # Insert new data
    dataset.insert(7, 'x', localPos[:, 0])
    dataset.insert(8, 'y', localPos[:, 1])
    dataset.insert(9, 'z', localPos[:, 2])
    dataset.insert(10, 'bx', geohashBlk[:, 0])
    dataset.insert(11, 'by', geohashBlk[:, 1])
    dataset.insert(12, 'bz', geohashBlk[:, 2])
    outputPath = os.path.join(outputPath, fileName)
    tqdmHandle.set_description(f"Writing CSV to {outputPath}")
    # Export the CSV and write the stat
    dataset.to_csv(outputPath)
    stat.loc[0, 'FilteredPings'] = dataset.shape[0]
    stat.loc[0, 'AccErrPings'] = np.sum(errPings)
    stat.loc[0, 'AltErrPings'] = np.sum(~validAlt)
    stat.loc[0, 'latitudeLb'] = wgsMapLb[0]
    stat.loc[0, 'longitudeLb'] = wgsMapLb[1]
    stat.loc[0, 'altitudeLb'] = wgsMapLb[2]
    stat.loc[0, 'xLb'] = mapLb[0]
    stat.loc[0, 'yLb'] = mapLb[1]
    stat.loc[0, 'zLb'] = mapLb[2]
    return stat

if __name__ == '__main__':
    args = parser.parse_args()
    blockSize = np.array((args.sizeX, args.sizeY, args.sizeZ))
    os.makedirs(args.outputDir, exist_ok=True)

    # Find all the CSV files
    csvFiles = []
    for path, _, files in os.walk(args.inputDir):
        for file in files:
            if file[-4:] == '.csv':
                csvFiles.append((path, file))
    print(f"Found {len(csvFiles)} files under {args.inputDir}")

    # Run!
    fileLoader = tqdm(csvFiles)
    stat = None
    for p, f in fileLoader:
        ret = preproc(p, f, args.outputDir, fileLoader, blockSize)
        if stat is None:
            stat = ret
        else:
            stat = pd.concat((stat, ret), ignore_index=True)
    
    # Export the stat data
    statPath = os.path.join(args.outputDir, "stat.csv")
    print(f"Export the stat to {statPath}")
    stat.to_csv(statPath)
    print("Done")
