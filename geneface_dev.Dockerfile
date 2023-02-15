FROM continuumio/miniconda3

RUN apt-get update && apt-get -y upgrade

RUN git clone https://github.com/zimonitrome/GeneFace
WORKDIR /geneface
    # && find . -type f -exec sed -i 's/\/home\/yezhenhui\/anaconda3\/envs\/geneface\/bin\/python/\/opt\/conda\/envs\/geneface\/bin\/python/g' {} +

# Step1. from docs/prepare_env/install_guide_nerf.md
RUN conda env create -f geneface_env.yml
RUN wget -P ./data_util/face_tracking/3DMM https://github.com/jadewu/3D-Human-Face-Reconstruction-with-3DMM-face-model-from-RGB-image/raw/main/BFM/01_MorphableModel.mat \
    && cd data_util/face_tracking \
    && conda run -n geneface python convert_BFM.py

RUN wget -P ./data_util/deepspeech_features https://github.com/mozilla/DeepSpeech/releases/download/v0.9.2/deepspeech-0.9.2-models.pbmm

# Step2. Download checkpoints
RUN wget https://github.com/yerfor/GeneFace/releases/download/v1.0.0/lrs3.zip \
    && wget https://github.com/yerfor/GeneFace/releases/download/v1.0.0/May.zip \
    && apt-get install unzip \
    && unzip lrs3.zip -d ./checkpoints/ \
    && unzip May.zip -d ./checkpoints/ \
    && rm lrs3.zip \
    && rm May.zip

# Step3. Download binarized dataset of "May.mp4"
RUN mkdir -p data/binary/videos/May/ \
    && conda run -n geneface gdown https://drive.google.com/uc?id=1i5ODDbDjoIMo-USLdDlLR5PCtCxbmAfy -O data/binary/videos/May/

# # Step4. 
# RUN apt-get install libsndfile1-dev
#     && export PYTHONPATH=./
#     && gdown https://drive.google.com/uc?id=1e-ezLkfixD97AYKca2VdtqN8wuKG9QJL -O data_util/BFM_models/
#     && bash scripts/infer_postnet.sh
#     && bash scripts/infer_postnet.sh
#     && apt-get install ffmpeg libsm6 libxext6 -y
#     && cp infer_out/May/pred_lm3d/zozo.npy data/raw/val_wavs/zozo_16k_deepspeech.npy
#     && bash scripts/infer_lm3d_nerf.sh

# OR custom NeRF params
# python inference/nerfs/lm3d_nerf_infer.py     --config=checkpoints/May/lm3d_nerf_torso/config.yaml     --hparams=infer_audio_source_name=data/raw/val_wavs/zozo.wav,infer_cond_name=infer_out/May/pred_lm3d/zozo.npy,infer_out_video_name=infer_out/May/pred_video/zozo.mp4,n_samples_per_ray=16,n_samples_per_ray_fine=32     --reset

# docker build -f .\geneface.Dockerfile -t geneface .
# docker run --gpus all geneface