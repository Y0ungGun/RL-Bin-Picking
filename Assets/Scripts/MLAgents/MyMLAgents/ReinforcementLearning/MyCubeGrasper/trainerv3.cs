using System;
using UnityEngine;
using Unity.MLAgents;
using Random = UnityEngine.Random;
using System.Collections;
using MyMLAgents.Utilities;
using static UnityEngine.GraphicsBuffer;
using Unity.VisualScripting;
using Unity.MLAgents.Sensors;
using System.Collections.Generic;
using Unity.MLAgents.Actuators;
using System.Linq;
using TMPro;

namespace MyMLAgents
{
    public class trainer3 : Agent
    {
        public Transform EndEffector;

        public int AgentID;

        private Camera cam;
        private Camera depthCamera;
        private GameObject Objects;
        private GameObject target;
        private Camera agentCamera;
        private RenderTexture renderTxt;
        private CloseTargetGripper closeTargetGripper;
        private ArticulationBody[] links;
        private ArticulationBody[] grips;
        private CubeSpawn cs;

        private float[] targetBBInfo;
        private float Episode = 0;
        private float EpisodeReward = 0;
        private int EpisodeLength;
        private float _w = 0.5f;
        private float _reward;
        private float r_dist;
        private float x_ = 0;
        private float y_ = 0;
        private bool ReadyToObserve = false;    
        private bool isActionInProgress = true;
        private bool? _success = null;
        void Start()
        {
            int.TryParse(transform.parent.gameObject.name.Substring(5), out AgentID);
            links = Utils.GetLinks(transform);
            grips = Utils.GetGrips(transform);
            Random.InitState(Guid.NewGuid().GetHashCode());
            cam = transform.parent.GetComponentsInChildren<Transform>().FirstOrDefault(t => t.name == "IntelCamera")?.GetComponent<Camera>();
            depthCamera = transform.parent.GetComponentsInChildren<Transform>().FirstOrDefault(t => t.name == "IntelCameraDepth")?.GetComponent<Camera>();
            closeTargetGripper = transform.parent.GetComponentsInChildren<Transform>().FirstOrDefault(t => t.name == "GripperControl")?.GetComponent<CloseTargetGripper>();
            agentCamera = transform.parent.GetComponentsInChildren<Transform>().FirstOrDefault(t => t.name == "TargetCam")?.GetComponentInChildren<Camera>();
            cs = gameObject.GetComponent<CubeSpawn>();  


            renderTxt = new RenderTexture(120, 120, 24);
            agentCamera.targetTexture = renderTxt;
        }
        public override void OnEpisodeBegin()
        {
            Episode = Episode + 1f;
            SetInitial();
            cs.SpawnCubes();
            StartCoroutine(StartwithDelay());
        }
        private IEnumerator StartwithDelay()
        {
            Utils.MoveToInitialPosition(transform);
            yield return new WaitForSeconds(4.0f);
            Objects = transform.parent.Find("Objects")?.gameObject;
            EpisodeLength = Objects.transform.childCount;
            Utils.FreezeObjects(Objects);
            Utils.UnFreezeObjects(Objects);
            yield return new WaitForSeconds(1.0f);
            ReadyToObserve = true; isActionInProgress = false;
        }
        public override void CollectObservations(VectorSensor sensor)
        {
            if (ReadyToObserve)
            {
                cs.DeleteOutlier(Objects);
                _reward = 0f;

                isActionInProgress = true;
                float[] featureVector = trainerUtils.DetectOBJv3(this, cam, _success);
                if (featureVector != null && featureVector.Length == 66)
                {
                    float[] obsVec = new float[64];
                    Array.Copy(featureVector, 0, obsVec, 0, 64);
                    sensor.AddObservation(obsVec);

                    x_ = featureVector[64];
                    y_ = featureVector[65];
                }
                else
                {
                    Debug.LogWarning("Feature vector is null or has wrong length.");
                    EndEpisode();
                }
                ReadyToObserve = false;
            }

        }
        public override void OnActionReceived(ActionBuffers actions)
        {
            isActionInProgress = true;
            StartCoroutine(PerformAction(actions));
        }
        private IEnumerator PerformAction(ActionBuffers actions)
        {
            int x_offset = (int)(AgentID / 8) * 20;
            int z_offset = - (AgentID % 8) * 15;
            Vector3 TargetPosition = trainerUtils.GetWorldXYZv3(x_, y_, depthCamera);

            target = Utils.FindTarget(Objects, TargetPosition.x, TargetPosition.z);
            //Debug.Log($"WorldPosition: x:{TargetPosition.x}, y: {TargetPosition.y}, z: {TargetPosition.z}");
            //Debug.Log($"Received Action: {actions.ContinuousActions[0]}, {actions.ContinuousActions[1]}, {actions.ContinuousActions[2]}, {actions.ContinuousActions[3]}, {actions.ContinuousActions[4]}, {actions.ContinuousActions[5]}");
            yield return new WaitForSeconds(1f);
            float x = TargetPosition.x + x_offset + _w * actions.ContinuousActions[0];
            float y = TargetPosition.z + z_offset + _w * actions.ContinuousActions[1];
            float z = TargetPosition.y + 0.2f + 0.2f * _w * actions.ContinuousActions[2];
            x = 0.1f * x;
            y = 0.1f * y;
            z = 0.1f * z;
            float rx = actions.ContinuousActions[3] * 1.5f;
            float ry = actions.ContinuousActions[4] * 0.75f + 3.14f;
            float rz = actions.ContinuousActions[5] * 0.75f + 1.57f;

            // Move To Target
            List<double> MoveTarget = trainerUtils.GetMArray(x, y, z, rx, ry, rz, 2.0f, links);
            for (int i = 0; i < MoveTarget.Count / 6; i++)
            {
                try
                {
                    trainerUtils.SetEachJointPositions(MoveTarget, i, links);
                }
                catch
                {
                    continue;
                }
                yield return new WaitForSeconds(0.05f);
            }
            yield return new WaitForSeconds(0.1f);

            // Move Downward
            Vector3 XYZDown = Utils.LocalMovement(x, y, z, rx, ry, rz, true, EndEffector);
            
            List<double> MoveDown = trainerUtils.GetMArray(XYZDown.x, XYZDown.y, XYZDown.z, rx, ry, rz, 1.0f, links);
            for (int j = 0; j < MoveDown.Count / 6; j++)
            {
                try
                {
                    trainerUtils.SetEachJointPositions(MoveDown, j, links);
                }
                catch
                {
                    continue;
                }
                yield return new WaitForSeconds(0.05f);
            }
            yield return new WaitForSeconds(0.1f);
            r_dist = EvaluateDistance();

            // Close Gripper
            closeTargetGripper.ButtonClicked = true;
            yield return new WaitForSeconds(3.5f);

            // Move Upward
            Vector3 XYZUp = Utils.LocalMovement(x, y, z, rx, ry, rz, false, EndEffector);
            List<double> MoveUp = trainerUtils.GetJArray(0, 0, 1.57f, 0, 1.57f, 0, 2.0f, links);
            for (int j = 0; j < MoveUp.Count / 6; j++)
            {
                try
                {
                    trainerUtils.SetEachJointPositions(MoveUp, j, links);
                }
                catch
                {
                    continue;
                }
                yield return new WaitForSeconds(0.05f);
            }
            yield return new WaitForSeconds(0.2f);
            
            EvaluateReward();
            yield return new WaitForSeconds(0.5f);
            Destroy(target);
            closeTargetGripper.ButtonClicked = false;
            Utils.MoveToInitialPosition(transform);
            yield return new WaitForSeconds(0.5f);

            isActionInProgress = false;
            if (Objects.transform.childCount == 0)
            {
                Debug.Log($"Reward for Episode{Episode}: {EpisodeReward}");
                EndEpisode();
            }

            ReadyToObserve = true;
        }
        private float EvaluateDistance()
        {
            float horizontalDistance = Vector2.Distance(new Vector2(EndEffector.position.x, EndEffector.position.z), new Vector2(target.transform.position.x, target.transform.position.z));
            float verticalDistance = Mathf.Abs(EndEffector.position.y - target.transform.position.y);
            float distnaceToTarget = Vector3.Distance(EndEffector.position, target.transform.position);
            if (horizontalDistance < 0.1f && verticalDistance < 0.1f)
            {
                //_reward += 3.0f;
            }

            return distnaceToTarget;
        }
        private void EvaluateReward()
        {
            //_reward += -r_dist;


            if (target.transform.position.y >= 0.3f)
            {
                _reward += 1.0f;
                EpisodeReward += 1.0f;
                _success = true;
            }
            else { _success = false; }

            SetReward(_reward);
        }
        private void SetInitial()
        {
            EpisodeReward = 0;
            _reward = 0;
            isActionInProgress = true;
            closeTargetGripper.ButtonClicked = false;
            ReadyToObserve = false;
        }
        public bool GetisActionInProgress()
        { return isActionInProgress; }
    }

}
