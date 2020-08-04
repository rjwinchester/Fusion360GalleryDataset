"""

Reconstruction Graph
Generates a graph data structure using a face adjacency graph of B-Rep topology

"""

import adsk.core
import adsk.fusion
import traceback
import json
import os
import sys
import time
import copy
from pathlib import Path
from importlib import reload
import unittest
import math

import name
import geometry
import exporter
import serialize
import exceptions
from logger import Logger
reload(name)
reload(geometry)


class Regraph():
    """Reconstruction Graph generation"""

    def __init__(self, logger=None, mode="PerExtrude"):
        self.logger = logger
        if self.logger is None:
            self.logger = Logger()
        # References to the Fusion design
        self.app = adsk.core.Application.get()
        self.design = adsk.fusion.Design.cast(self.app.activeProduct)
        self.product = self.app.activeProduct
        self.timeline = self.app.activeProduct.timeline
        # Data structure to return
        self.data = {
            "graphs": [],
            "sequences": [],
            "status": []
        }
        # Cache of the extrude face label information
        self.face_cache = {}
        # Cache of the edge information
        self.edge_cache = {}
        # The sequence of nodes and edges that become explained
        self.sequence = []
        # The cache of the faces and edges seen so far
        self.sequence_cache = {
            "faces": set(),
            "edges": set()
        }
        # Current extrude index
        self.current_extrude_index = 0
        # Current overall action index
        self.current_action_index = 0
        # The mode we want
        self.mode = mode

    # -------------------------------------------------------------------------
    # GENERATE
    # -------------------------------------------------------------------------

    def generate(self, target_component=None):
        """Generate graphs from the design in the timeline"""
        self.target_component = target_component
        if self.target_component is None:
            self.target_component = self.design.rootComponent
        assert self.target_component.bRepBodies.count > 0
        # Iterate over the timeline and populate the face cache
        for timeline_object in self.timeline:
            if isinstance(timeline_object.entity, adsk.fusion.ExtrudeFeature):
                self.add_extrude_to_cache(timeline_object.entity)
        # Check that all faces have uuids
        for body in self.target_component.bRepBodies:
            for face in body.faces:
                face_uuid = name.get_uuid(face)
                assert face_uuid is not None
        prev_extrude_index = 0
        skip_reason = None
        # Next move the marker to after each extrude and export
        for timeline_object in self.timeline:
            if isinstance(timeline_object.entity, adsk.fusion.ExtrudeFeature):
                self.timeline.markerPosition = timeline_object.index + 1
                extrude = timeline_object.entity
                supported, unsupported_reason = self.is_extrude_supported(extrude)
                if not supported:
                    self.timeline.markerPosition = prev_extrude_index
                    skip_reason = unsupported_reason
                    break
                # Populate the cache again
                self.add_extrude_to_cache(extrude)
                self.add_extrude_edges_to_cache()
                self.generate_extrude(extrude)
                prev_extrude_index = self.timeline.markerPosition
        if skip_reason is None:
            self.generate_last()
        else:
            self.data["status"].append(skip_reason)
        return self.data

    def generate_extrude(self, extrude):
        """Generate a graph after each extrude as reconstruction takes place"""
        # If we are exporting per curve
        if self.mode == "PerFace":
            self.add_extrude_to_sequence(extrude)
        elif self.mode == "PerExtrude":
            graph = self.get_graph()
            self.data["graphs"].append(graph)
            self.data["status"].append("Success")
        self.current_extrude_index += 1

    def generate_last(self):
        """Export after the full reconstruction"""
        # The last extrude
        if self.mode == "PerFace":
            # Only export if we had some valid extrudes
            if self.current_extrude_index > 0:
                graph = self.get_graph()
                self.data["graphs"].append(graph)
                bbox = geometry.get_bounding_box(self.target_component)
                bbox_data = serialize.bounding_box3d(bbox)
                seq_data = {
                    "sequence": self.sequence,
                    "properties": {
                        "bounding_box": bbox_data
                    }
                }
                self.data["sequences"].append(seq_data)
                self.data["status"].append("Success")

    # -------------------------------------------------------------------------
    # DATA CACHING
    # -------------------------------------------------------------------------

    def get_extrude_operation(self, extrude_operation):
        """Get the extrude operation as short string and regular string"""
        operation = serialize.feature_operation(extrude_operation)
        operation_short = operation.replace("FeatureOperation", "")
        assert operation_short != "NewComponent"
        if operation_short == "NewBody" or operation_short == "Join":
            operation_short = "Extrude"
        return operation, operation_short

    def add_extrude_to_cache(self, extrude):
        """Add the data from the latest extrude to the cache"""
        # First toggle the previous extrude last_operation label
        for face_data in self.face_cache.values():
            face_data["last_operation_label"] = False
        operation, operation_short = self.get_extrude_operation(extrude.operation)
        self.add_extrude_faces_to_cache(extrude.startFaces, operation_short, "Start")
        self.add_extrude_faces_to_cache(extrude.endFaces, operation_short, "End")
        self.add_extrude_faces_to_cache(extrude.sideFaces, operation_short, "Side")

    def add_extrude_faces_to_cache(self, extrude_faces, operation_short, extrude_face_location):
        """Update the extrude face cache with the recently added faces"""
        for face in extrude_faces:
            face_uuid = name.set_uuid(face)
            assert face_uuid is not None
            # We will have split faces with the same uuid
            # So we need to update them
            # assert face_uuid not in self.face_cache
            self.face_cache[face_uuid] = {
                # "timeline_label": self.current_extrude_index / self.extrude_count,
                "operation_label": f"{operation_short}{extrude_face_location}",
                "last_operation_label": True
            }

    def add_extrude_edges_to_cache(self):
        """Update the edge cache with the latest extrude"""
        concave_edge_cache = set()
        for body in self.target_component.bRepBodies:
            temp_ids = name.get_temp_ids_from_collection(body.concaveEdges)
            concave_edge_cache.update(temp_ids)
        for body in self.target_component.bRepBodies:
            for face in body.faces:
                for edge in face.edges:
                    assert edge.faces.count == 2
                    edge_uuid = name.set_uuid(edge)
                    edge_concave = edge.tempId in concave_edge_cache
                    assert edge_uuid is not None
                    self.edge_cache[edge_uuid] = {
                        "temp_id": edge.tempId,
                        "convexity": self.get_edge_convexity(edge, edge_concave),
                        "source": name.get_uuid(edge.faces[0]),
                        "target": name.get_uuid(edge.faces[1])
                    }

    def add_extrude_to_sequence(self, extrude):
        """Add the extrude operation to the sequence"""
        adsk.doEvents()
        # Look for a start or end face with a single face
        start_end_face_set = False
        if (extrude.startFaces.count == 1 and
           extrude.endFaces.count == 1):
            # If we have both a start face and an end face
            # we can't tell which face will remain intact so
            # we skip to the end of the design
            # and check it still exists
            prev_timeline_index = self.timeline.markerPosition
            self.timeline.moveToEnd()
            # If either start or end is absent
            # assign the other if we can
            if extrude.startFaces.count == 0:
                if extrude.endFaces.count > 0:
                    start_face = extrude.endFaces[0]
                    end_faces = extrude.startFaces
                    start_end_flipped = True
                    start_end_face_set = True
                self.timeline.markerPosition = prev_timeline_index
            elif extrude.endFaces.count == 0:
                if extrude.startFaces.count > 0:
                    start_face = extrude.startFaces[0]
                    end_faces = extrude.endFaces
                    start_end_flipped = False
                    start_end_face_set = True
                self.timeline.markerPosition = prev_timeline_index
            # If we have both start and end then pick the larger one
            # which has not been trimmed/split
            if (extrude.startFaces.count > 0 and
                extrude.endFaces.count > 0):
                    sf = extrude.startFaces[0]
                    ef = extrude.endFaces[0]
                    sf_area = sf.area
                    ef_area = ef.area
                    self.timeline.markerPosition = prev_timeline_index
                    # If both start and end are the same size
                    # then we want to skip out here
                    # and let the regular priority order take place
                    if not math.isclose(sf_area, ef_area, abs_tol=0.01):
                        if sf_area > ef_area:
                            start_face = extrude.startFaces[0]
                            end_faces = extrude.endFaces
                            start_end_flipped = False
                        else:
                            start_face = extrude.endFaces[0]
                            end_faces = extrude.startFaces
                            start_end_flipped = True
                        start_end_face_set = True
        # If we haven't yet decided, prioritize the start face
        if not start_end_face_set:
            if extrude.startFaces.count == 1:
                start_face = extrude.startFaces[0]
                end_faces = extrude.endFaces
                start_end_flipped = False
            elif extrude.endFaces.count == 1:
                start_face = extrude.endFaces[0]
                end_faces = extrude.startFaces
                start_end_flipped = True
        assert start_face is not None
        start_face_uuid = name.get_uuid(start_face)
        assert start_face_uuid is not None
        # Add the face and edges that we extrude from
        self.sequence_cache["faces"].add(start_face_uuid)
        for edge in start_face.edges:
            edge_uuid = name.get_uuid(edge)
            assert edge_uuid is not None
            self.sequence_cache["edges"].add(edge_uuid)
        extrude_from_sequence_entry = {
            "action": start_face_uuid,
            "faces": list(self.sequence_cache["faces"]),
            "edges": list(self.sequence_cache["edges"])
        }
        self.sequence.append(extrude_from_sequence_entry)

        if end_faces.count > 0:
            # If we have a face to extrude to, lets use it
            end_face = end_faces[0]
        else:
            # Or we need to find an end face to extrude to
            # that is on coplanar to the end of the extrude
            if start_end_flipped:
                end_plane = self.get_extrude_start_plane(extrude)
            else:
                end_plane = self.get_extrude_end_plane(extrude)
            # Search for faces that are coplanar
            end_face = self.get_coplanar_face(end_plane)
        assert end_face is not None
        end_face_uuid = name.get_uuid(end_face)
        assert end_face_uuid is not None
        # print(f"End face: {end_face.tempId}")
        # Add the face and edges for everything that was extruded
        for face in extrude.faces:
            face_uuid = name.get_uuid(face)
            assert face_uuid is not None
            self.sequence_cache["faces"].add(face_uuid)
            for edge in face.edges:
                edge_uuid = name.get_uuid(edge)
                assert edge_uuid is not None
                self.sequence_cache["edges"].add(edge_uuid)
        extrude_to_sequence_entry = {
            "action": end_face_uuid,
            "faces": list(self.sequence_cache["faces"]),
            "edges": list(self.sequence_cache["edges"])
        }
        self.sequence.append(extrude_to_sequence_entry)

    # -------------------------------------------------------------------------
    # FEATURES
    # -------------------------------------------------------------------------

    def get_edge_convexity(self, edge, is_concave):
        # is_concave = self.is_concave_edge(edge.tempId)
        is_tc = geometry.are_faces_tangentially_connected(edge.faces[0], edge.faces[1])
        convexity = "Convex"
        # edge_data["convex"] = self.is_convex_edge(edge.tempId)
        if is_concave:
            convexity = "Concave"
        elif is_tc:
            convexity = "Smooth"
        return convexity

    def get_trimming_mask(self, pt, body):
        """Return a trimming mask value indicating if a point should be masked or not"""
        containment = body.pointContainment(pt)
        binary_containment = 1
        if containment == adsk.fusion.PointContainment.PointOutsidePointContainment:
            binary_containment = 0
        elif containment == adsk.fusion.PointContainment.UnknownPointContainment:
            binary_containment = 0
        return binary_containment

    def linspace(self, start, stop, n):
        if n == 1:
            yield stop
            return
        h = (stop - start) / (n - 1)
        for i in range(n):
            yield start + h * i

    def get_edge_parameter_features(self, edge):
        param_features = {}
        samples = 10
        evaluator = edge.evaluator
        result, start_param, end_param = evaluator.getParameterExtents()
        assert result
        parameters = list(self.linspace(start_param, end_param, samples))
        result, points = evaluator.getPointsAtParameters(parameters)
        assert result
        param_features["points"] = []
        for pt in points:
            param_features["points"].append(pt.x)
            param_features["points"].append(pt.y)
            param_features["points"].append(pt.z)
        return param_features

    def get_face_parameter_features(self, face):
        param_features = {}
        samples = 4
        evaluator = face.evaluator
        range_bbox = evaluator.parametricRange()
        u_min = range_bbox.minPoint.x
        u_max = range_bbox.maxPoint.x
        v_min = range_bbox.minPoint.y
        v_max = range_bbox.maxPoint.y
        u_params = list(self.linspace(u_min, u_max, samples+2))[1:-1]
        v_params = list(self.linspace(v_min, v_max, samples+2))[1:-1]
        params = []
        for u in range(samples):
            for v in range(samples):
                pt = adsk.core.Point2D.create(u_params[u], v_params[v])
                params.append(pt)
        result, points = evaluator.getPointsAtParameters(params)
        result, normals = evaluator.getNormalsAtParameters(params)
        assert result
        param_features["points"] = []
        param_features["normals"] = []
        param_features["trimming_mask"] = []
        for i, pt in enumerate(points):
            param_features["points"].append(pt.x)
            param_features["points"].append(pt.y)
            param_features["points"].append(pt.z)
            normal = normals[i]
            param_features["normals"].append(normal.x)
            param_features["normals"].append(normal.y)
            param_features["normals"].append(normal.z)
            trim_mask = self.get_trimming_mask(pt, face.body)
            param_features["trimming_mask"].append(trim_mask)
        return param_features

    # -------------------------------------------------------------------------
    # FILTER
    # -------------------------------------------------------------------------

    def is_extrude_supported(self, extrude):
        """Check if this is a supported extrude for export"""
        reason = None
        if extrude.operation == adsk.fusion.FeatureOperations.IntersectFeatureOperation:
            reason = "Extrude has intersect operation"
        elif self.is_extrude_tapered(extrude):
            reason = "Extrude has taper"
        # elif extrude.extentType != adsk.fusion.FeatureExtentTypes.OneSideFeatureExtentType:
        #     reason = "Extrude is not one sided"
        elif self.mode == "PerFace":
            # If we have a cut/intersect operation we want to use what we have
            # and export it
            if extrude.operation == adsk.fusion.FeatureOperations.CutFeatureOperation:
                reason = "Extrude has cut operation"
            # If we don't have a single extrude start/end face
            elif extrude.endFaces.count != 1 and extrude.startFaces.count != 1:
                reason = "Extrude doesn't have a single start or end face"
        if reason is not None:
            self.logger.log(f"Skipping {extrude.name}: {reason}")
            return False, reason
        else:
            return True, None

    def is_design_supported(self, json_data):
        """Check the raw json data to see if this is a supported design for export"""
        if not isinstance(json_data, dict):
            with open(json_data, encoding="utf8") as f:
                json_data = json.load(f, object_pairs_hook=OrderedDict)
        reason = None
        timeline = json_data["timeline"]
        entities = json_data["entities"]
        for timeline_object in timeline:
            entity_uuid = timeline_object["entity"]
            entity_index = timeline_object["index"]
            entity = entities[entity_uuid]
            if entity["type"] == "ExtrudeFeature":
                if entity["operation"] == "IntersectFeatureOperation":
                    reason = "Extrude has intersect operation"
                    break
                elif ("taper_angle" in entity["extent_one"] and
                     entity["extent_one"]["taper_angle"]["value"] != 0):
                        reason = "Extrude has taper"
                        break
                # elif entity["extent_type"] != "OneSideFeatureExtentType":
                #     reason = "Extrude is not one sided"
                #     break
                elif self.mode == "PerFace":
                    if entity["operation"] == "CutFeatureOperation":
                        reason = "Extrude has cut operation"
                        break
                    elif len(entity["extrude_start_faces"]) != 1 and len(entity["extrude_end_faces"]) != 1:
                        reason = "Extrude doesn't have a single start or end face"
                        break
        if reason is not None:
            self.logger.log(f"Skipping {json_data['metadata']['parent_project']} early: {reason}")
            return False, reason
        else:
            return True, None 

    def is_extrude_tapered(self, extrude):
        if extrude.extentOne is not None:
            if isinstance(extrude.extentOne, adsk.fusion.DistanceExtentDefinition):
                if extrude.taperAngleOne is not None:
                    if extrude.taperAngleOne.value is not None and extrude.taperAngleOne.value != "":
                        if extrude.taperAngleOne.value != 0:
                            return True
        # Check the second extent if needed
        if (extrude.extentType ==
                adsk.fusion.FeatureExtentTypes.TwoSidesFeatureExtentType):
            if extrude.extentTwo is not None:
                if isinstance(extrude.extentTwo, adsk.fusion.DistanceExtentDefinition):
                    if extrude.taperAngleTwo is not None:
                        if extrude.taperAngleTwo.value is not None and extrude.taperAngleTwo.value != "":
                            if extrude.taperAngleTwo.value != 0:
                                return True
        return False

    # -------------------------------------------------------------------------
    # GRAPH CONSTRUCTION
    # -------------------------------------------------------------------------

    def get_empty_graph(self):
        """Get an empty graph to start"""
        return {
            "directed": False,
            "multigraph": False,
            "graph": {},
            "nodes": [],
            "links": []
        }

    def get_graph(self):
        """Get a graph data structure for bodies"""
        graph = self.get_empty_graph()
        for body in self.target_component.bRepBodies:
            for face in body.faces:
                if face is not None:
                    face_data = self.get_face_data(face)
                    graph["nodes"].append(face_data)

            for edge in body.edges:
                if edge is not None:
                    edge_data = self.get_edge_data(edge)
                    graph["links"].append(edge_data)
        return graph

    def get_face_data(self, face):
        """Get the features for a face"""
        face_uuid = name.get_uuid(face)
        assert face_uuid is not None
        face_metadata = self.face_cache[face_uuid]
        if self.mode == "PerExtrude":
            return self.get_face_data_per_extrude(face, face_uuid, face_metadata)
        elif self.mode == "PerFace":
            return self.get_face_data_per_face(face, face_uuid, face_metadata)

    def get_common_face_data(self, face, face_uuid):
        """Get common edge data"""
        face_data = {}
        face_data["id"] = face_uuid
        return face_data

    def get_face_data_per_extrude(self, face, face_uuid, face_metadata):
        """Get the features for a face for a per extrude graph"""
        face_data = self.get_common_face_data(face, face_uuid)
        face_data["surface_type"] = serialize.surface_type(face.geometry)
        # face_data["surface_type_id"] = face.geometry.surfaceType
        face_data["area"] = face.area
        normal = geometry.get_face_normal(face)
        face_data["normal_x"] = normal.x
        face_data["normal_y"] = normal.y
        face_data["normal_z"] = normal.z
        # face_data["normal_length"] = normal.length
        parameter_result, parameter_at_point = face.evaluator.getParameterAtPoint(face.pointOnFace)
        assert parameter_result
        curvature_result, max_tangent, max_curvature, min_curvature = face.evaluator.getCurvature(parameter_at_point)
        assert curvature_result
        face_data["max_tangent_x"] = max_tangent.x
        face_data["max_tangent_y"] = max_tangent.y
        face_data["max_tangent_z"] = max_tangent.z
        # face_data["max_tangent_length"] = max_tangent.length
        face_data["max_curvature"] = max_curvature
        face_data["min_curvature"] = min_curvature
        # face_data["timeline_label"] = face_metadata["timeline_label"]
        face_data["operation_label"] = face_metadata["operation_label"]
        face_data["last_operation_label"] = face_metadata["last_operation_label"]
        return face_data

    def get_face_data_per_face(self, face, face_uuid, face_metadata):
        """Get the features for a face for a per curve graph"""
        face_data = self.get_common_face_data(face, face_uuid)
        face_data["surface_type"] = serialize.surface_type(face.geometry)
        face_param_feat = self.get_face_parameter_features(face)
        face_data.update(face_param_feat)
        return face_data

    def get_edge_data(self, edge):
        """Get the features for an edge"""
        edge_uuid = name.get_uuid(edge)
        assert edge_uuid is not None
        edge_metadata = self.edge_cache[edge_uuid]
        if self.mode == "PerExtrude":
            return self.get_edge_data_per_extrude(edge, edge_uuid, edge_metadata)
        elif self.mode == "PerFace":
            return self.get_edge_data_per_face(edge, edge_uuid, edge_metadata)

    def get_common_edge_data(self, edge_uuid, edge_metadata):
        """Get common edge data"""
        edge_data = {}
        edge_data["id"] = edge_uuid
        edge_data["source"] = edge_metadata["source"]
        edge_data["target"] = edge_metadata["target"]
        return edge_data

    def get_edge_data_per_extrude(self, edge, edge_uuid, edge_metadata):
        """Get the features for an edge for a per extrude graph"""
        edge_data = self.get_common_edge_data(edge_uuid, edge_metadata)
        edge_data["curve_type"] = serialize.curve_type(edge.geometry)
        # edge_data["curve_type_id"] = edge.geometry.curveType
        edge_data["length"] = edge.length
        # Create a feature for the edge convexity
        edge_data["convexity"] = edge_metadata["convexity"]
        edge_data["perpendicular"] = geometry.are_faces_perpendicular(edge.faces[0], edge.faces[1])
        point_on_edge = edge.pointOnEdge
        evaluator = edge.evaluator
        parameter_result, parameter_at_point = evaluator.getParameterAtPoint(point_on_edge)
        assert parameter_result
        curvature_result, direction, curvature = evaluator.getCurvature(parameter_at_point)
        edge_data["direction_x"] = direction.x
        edge_data["direction_y"] = direction.y
        edge_data["direction_z"] = direction.z
        # edge_data["direction_length"] = direction.length
        edge_data["curvature"] = curvature
        return edge_data

    def get_edge_data_per_face(self, edge, edge_uuid, edge_metadata):
        """Get the features for an edge for a per curve graph"""
        edge_data = self.get_common_edge_data(edge_uuid, edge_metadata)
        # edge_param_feat = self.get_edge_parameter_features(edge)
        # edge_data.update(edge_param_feat)
        return edge_data

    def get_extrude_start_plane(self, extrude):
        """Get the plane where the extrude starts"""
        extrude_offset = self.get_extrude_offset(extrude)
        sketch = extrude.profile.parentSketch
        sketch_normal = extrude.profile.plane.normal
        sketch_normal.transformBy(sketch.transform)
        sketch_origin = sketch.origin
        if extrude_offset != 0:
            sketch_origin = self.offset_point_by_distance(sketch_origin, sketch_normal, extrude_offset)
        return adsk.core.Plane.create(sketch_origin, sketch_normal)

    def get_extrude_end_plane(self, extrude):
        """Get the plane where the extrude ends"""
        plane = self.get_extrude_start_plane(extrude)
        extrude_distance = self.get_extrude_distance(extrude)
        plane.origin = self.offset_point_by_distance(plane.origin, plane.normal, extrude_distance)
        return plane

    def offset_point_by_distance(self, point, vector, distance):
        """Offset a point along a vector by a given distance"""
        point_vector = point.asVector()
        scale_vector = vector.copy()
        scale_vector.scaleBy(distance)
        point_vector.add(scale_vector)
        return point_vector.asPoint()

    def get_extrude_distance(self, extrude):
        """Get the extrude distance"""
        if extrude.extentType != adsk.fusion.FeatureExtentTypes.OneSideFeatureExtentType:
            raise exceptions.UnsupportedException(f"Unsupported Extent Type: {extrude.extentType}")
        if not isinstance(extrude.extentOne, adsk.fusion.DistanceExtentDefinition):
            raise exceptions.UnsupportedException(f"Unsupported Extent Definition: {extrude.extentOne.objectType}")
        return extrude.extentOne.distance.value

    def get_extrude_offset(self, extrude):
        """Get any offset from the sketch plane to the extrude"""
        start_extent = extrude.startExtent
        if isinstance(start_extent, adsk.fusion.ProfilePlaneStartDefinition):
            return 0
        elif isinstance(start_extent, adsk.fusion.OffsetStartDefinition):
            offset = start_extent.offset
            # If the ProfilePlaneWithOffsetDefinition is
            # associated with an existing feature
            if isinstance(offset, adsk.fusion.ModelParameter):
                return offset.value
            # If the ProfilePlaneWithOffsetDefinition object was created statically
            # and is not associated with a feature
            elif isinstance(offset, adsk.core.ValueInput):
                if offset.valueType == adsk.fusion.ValueTypes.RealValueType:
                    return offset.realValue
                elif value_input.valueType == adsk.fusion.ValueTypes.StringValueType:
                    return float(offset.stringValue)
        return 0

    def get_coplanar_face(self, plane):
        """Find a face that is coplanar to the given plane"""
        for body in self.target_component.bRepBodies:
            for face in body.faces:
                if isinstance(face.geometry, adsk.core.Plane):
                    is_coplanar = plane.isCoPlanarTo(face.geometry)
                    if is_coplanar:
                        return face
        return None


class RegraphTester(unittest.TestCase):
    """Reconstruction Graph tester to check for invalid data"""

    def __init__(self, mode="PerExtrude"):
        self.mode = mode
        unittest.TestCase.__init__(self)

    def test(self, graph_data):
        """Test the graph data structure returned by regraph"""
        if self.mode == "PerExtrude":
            for graph in graph_data["graphs"]:
                self.test_per_extrude_graph(graph)
        elif self.mode == "PerFace":
            if len(graph_data["sequences"]) > 0:
                self.assertEqual(len(graph_data["sequences"]), 1, msg="Only 1 per face sequence")
                self.assertEqual(len(graph_data["graphs"]), 1, msg="Only 1 per face graph")
                self.test_per_face_graph(graph_data["graphs"][0], graph_data["sequences"][0])

    def reconstruct(self, graph_data, target_component=None):
        """Reconstruct and test it matches the target"""
        regraph_reconstructor = RegraphReconstructor(graph_data, target_component)
        regraph_reconstructor.reconstruct()
        # Compare the ground truth with the reconstruction
        gt = regraph_reconstructor.target_component
        rc = regraph_reconstructor.reconstruction.component
        self.test_reconstruction(gt, rc)
        regraph_reconstructor.remove()
    
    def test_per_extrude_graph(self, graph):
        """Test a per extrude graph"""
        self.assertIsNotNone(graph, msg="Graph is not None")
        self.assertIn("nodes", graph, msg="Graph has nodes")
        self.assertIn("links", graph, msg="Graph has links")
        self.assertGreaterEqual(len(graph["nodes"]), 3, msg="Graph nodes >= 3")
        self.assertGreaterEqual(len(graph["links"]), 2, msg="Graph links >= 2")
        node_set = set()
        node_list = []
        for node in graph["nodes"]:
            self.assertIn("id", node, msg="Graph node has id")
            node_set.add(node["id"])
            node_list.append(node["id"])
        self.assertEqual(len(node_set), len(node_list), msg="Graph nodes are unique")
        for link in graph["links"]:
            self.assertIn("id", link, msg="Graph link has id")
            self.assertIn("source", link, msg="Graph link has source")
            self.assertIn(link["source"], node_set, msg="Graph link source in node set")
            self.assertIn("target", link, msg="Graph link has target")
            self.assertIn(link["target"], node_set, msg="Graph link target in node set")

    def test_per_face_graph(self, graph, sequence):
        """Test a per face graph"""
        # Target graph
        self.assertIsNotNone(graph, msg="Graph is not None")
        self.assertIn("nodes", graph, msg="Graph has nodes")
        self.assertIsInstance(graph["nodes"], list, msg="Nodes is list")
        self.assertIn("links", graph, msg="Graph has links")
        self.assertIsInstance(graph["links"], list, msg="Links is list")
        self.assertGreaterEqual(len(graph["nodes"]), 3, msg="Graph nodes >= 3")
        self.assertGreaterEqual(len(graph["links"]), 2, msg="Graph links >= 3")
        node_set = set()
        node_list = []
        for node in graph["nodes"]:
            self.assertIn("id", node, msg="Graph node has id")
            node_set.add(node["id"])
            node_list.append(node["id"])
        self.assertEqual(len(node_set), len(node_list), msg="Graph nodes are unique")
        link_set = set()
        for link in graph["links"]:
            self.assertIn("id", link, msg="Graph link has id")
            link_set.add(link["id"])
            self.assertIn("source", link, msg="Graph link has source")
            self.assertIn(link["source"], node_set, msg="Graph link source in node set")
            self.assertIn("target", link, msg="Graph link has target")
            self.assertIn(link["target"], node_set, msg="Graph link target in node set")        
        # Sequence
        self.assertIsNotNone(sequence, msg="Sequence is not None")
        self.assertIn("sequence", sequence, msg="Sequence has sequence")
        self.assertGreaterEqual(len(sequence["sequence"]), 2, msg="Sequence length >= 2")
        self.assertEqual(len(sequence["sequence"]) % 2, 0, msg="Sequence length is multiple of 2")
        for seq in sequence["sequence"]:
            self.assertIn("action", seq, msg="Sequence element has action")
            self.assertIn(seq["action"], node_set, msg="Action is in target nodes")
            self.assertIn("faces", seq, msg="Sequence element has faces")
            for face in seq["faces"]:
                self.assertIn(face, node_set, msg="Face is in target nodes")
            self.assertIn("edges", seq, msg="Sequence element has edges")
            for edge in seq["edges"]:
                self.assertIn(edge, link_set, msg="Edge is in target links")
        # Properties
        self.assertIn("properties", sequence, msg="Sequence has properties")
        self.assertIn("bounding_box", sequence["properties"], msg="Properties has bounding_box")

    def test_reconstruction(self, gt, rc, places=1):
        """Test the reconstruction"""
        self.assertEqual(
            len(gt.bRepBodies),
            len(rc.bRepBodies),
            msg="Same number of bodies"
        )
        self.assertEqual(
            geometry.get_face_count(gt),
            geometry.get_face_count(rc),
            msg="Same number of faces"
        )
        self.assertEqual(
            geometry.get_edge_count(gt),
            geometry.get_edge_count(rc),
            msg="Same number of edges"
        )
        gt_bbox = geometry.get_bounding_box(gt)
        rc_bbox = geometry.get_bounding_box(rc)
        self.assertAlmostEqual(
            rc_bbox.maxPoint.x,
            gt_bbox.maxPoint.x,
            places=places, msg="bounding_box_max_x"
        )
        self.assertAlmostEqual(
            rc_bbox.maxPoint.y,
            gt_bbox.maxPoint.y,
            places=places,
            msg="bounding_box_max_y"
        )
        self.assertAlmostEqual(
            rc_bbox.maxPoint.z,
            gt_bbox.maxPoint.z,
            places=places,
            msg="bounding_box_max_z"
        )
        self.assertAlmostEqual(
            rc_bbox.minPoint.x,
            gt_bbox.minPoint.x,
            places=places,
            msg="bounding_box_min_x"
        )
        self.assertAlmostEqual(
            rc_bbox.minPoint.y,
            gt_bbox.minPoint.y,
            places=places,
            msg="bounding_box_min_y"
        )
        self.assertAlmostEqual(
            rc_bbox.minPoint.z,
            gt_bbox.minPoint.z,
            places=places,
            msg="bounding_box_min_z"
        )
        self.assertFalse(
            math.isinf(rc_bbox.maxPoint.x),
            msg="bounding_box_max_x != inf"
        )
        self.assertFalse(
            math.isinf(rc_bbox.maxPoint.y),
            msg="bounding_box_max_y != inf"
        )
        self.assertFalse(
            math.isinf(rc_bbox.maxPoint.z),
            msg="bounding_box_max_z != inf"
        )
        self.assertFalse(
            math.isinf(rc_bbox.minPoint.x),
            msg="bounding_box_min_x != inf"
        )
        self.assertFalse(
            math.isinf(rc_bbox.minPoint.y),
            msg="bounding_box_min_y != inf"
        )
        self.assertFalse(
            math.isinf(rc_bbox.minPoint.z),
            msg="bounding_box_min_z != inf"
        )


class RegraphReconstructor():
    """Reconstruct the graph to test it matches the target"""

    def __init__(self, graph_data, target_component=None):
        self.app = adsk.core.Application.get()
        self.design = adsk.fusion.Design.cast(self.app.activeProduct)
        self.target_component = target_component
        if self.target_component is None:
            self.target_component = self.design.rootComponent
        self.graph = graph_data["graphs"][0]
        self.sequence = graph_data["sequences"][0]

    def reconstruct(self):
        """Reconstruct from the sequence of faces"""
        # Create a reconstruction component that we create geometry in
        self.reconstruction = self.design.rootComponent.occurrences.addNewComponent(
            adsk.core.Matrix3D.create()
        )
        self.reconstruction.component.name = "Reconstruction"
        uuid_to_face_map = self.get_uuid_to_face_map()
        extrude_count = int(len(self.sequence["sequence"]) * 0.5)
        for extrude_index in range(extrude_count):
            start_index = extrude_index * 2
            end_index = extrude_index * 2 + 1
            start_face = self.get_face_from_sequence_index(start_index, uuid_to_face_map)
            end_face = self.get_face_from_sequence_index(end_index, uuid_to_face_map)
            self.add_extrude(start_face, end_face)

    def get_face_from_sequence_index(self, seq_index, uuid_to_face_map):
        """Get a face from an index in the sequence"""
        face_uuid = self.sequence["sequence"][seq_index]["action"]
        indices = uuid_to_face_map[face_uuid]
        body_index = indices["body_index"]
        face_index = indices["face_index"]
        body = self.target_component.bRepBodies[body_index]
        face = body.faces[face_index]
        return face

    def get_uuid_to_face_map(self):
        """As we have to find faces multiple times we first
            make a map between uuids and face indices"""
        uuid_to_face_map = {}
        for body_index, body in enumerate(self.target_component.bRepBodies):
            for face_index, face in enumerate(body.faces):
                face_uuid = name.get_uuid(face)
                assert face_uuid is not None
                uuid_to_face_map[face_uuid] = {
                    "body_index": body_index,
                    "face_index": face_index
                }
        return uuid_to_face_map

    def add_extrude(self, start_face, end_face):
        """Create an extrude from a start face to an end face"""
        # We generate the extrude bodies in the reconstruction component
        extrudes = self.reconstruction.component.features.extrudeFeatures
        operation = adsk.fusion.FeatureOperations.NewBodyFeatureOperation
        extrude_input = extrudes.createInput(start_face, operation)
        extent = adsk.fusion.ToEntityExtentDefinition.create(end_face, False)
        extrude_input.setOneSideExtent(extent, adsk.fusion.ExtentDirections.PositiveExtentDirection)
        extrude = extrudes.add(extrude_input)
        # The Fusion API  doesn't seem to be able to do join extrudes
        # that don't join to the goal body
        # so we make the bodies separate and then join them after the fact
        if self.reconstruction.component.bRepBodies.count > 1:
            combines = self.reconstruction.component.features.combineFeatures
            first_body = self.reconstruction.component.bRepBodies[0]
            tools = adsk.core.ObjectCollection.create()
            for body in extrude.bodies:
                tools.add(body)
            combine_input = combines.createInput(first_body, tools)
            combine = combines.add(combine_input)
        return extrude

    def remove(self):
        """Remove the reconstructed component"""
        self.reconstruction.deleteMe()
