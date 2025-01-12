import abc
import dataclasses
from typing import ClassVar, Optional, Tuple

import f90nml
import xarray as xr

from ndsl import Namelist, QuantityFactory, ndsl_log
from ndsl.comm.partitioner import get_tile_index
from ndsl.constants import X_DIM, X_INTERFACE_DIM, Y_DIM, Y_INTERFACE_DIM
from ndsl.grid import (
    AngleGridData,
    ContravariantGridData,
    DampingCoefficients,
    DriverGridData,
    GridData,
    HorizontalGridData,
    MetricTerms,
    VerticalGridData,
)
from ndsl.grid.stretch_transformation import direct_transform
from ndsl.stencils.testing import TranslateGrid, grid
from ndsl.typing import Communicator
from pace.registry import Registry


class GridInitializer(abc.ABC):
    @abc.abstractmethod
    def get_grid(
        self,
        quantity_factory: QuantityFactory,
        communicator: Communicator,
    ) -> Tuple[DampingCoefficients, DriverGridData, GridData]:
        ...


@dataclasses.dataclass
class GridInitializerSelector(GridInitializer):
    """
    Dataclass for selecting the implementation of GridInitializer to use.

    Used to circumvent the issue that dacite expects static class definitions,
    but we would like to dynamically define which GridInitializer to use. Does this
    by representing the part of the yaml specification that asks which initializer
    to use, but deferring to the implementation in that initializer when called.
    """

    type: str
    config: GridInitializer
    registry: ClassVar[Registry] = Registry()

    @classmethod
    def register(cls, type_name):
        return cls.registry.register(type_name)

    def get_grid(
        self,
        quantity_factory: QuantityFactory,
        communicator: Communicator,
    ) -> Tuple[DampingCoefficients, DriverGridData, GridData]:
        return self.config.get_grid(
            quantity_factory=quantity_factory,
            communicator=communicator,
        )

    @classmethod
    def from_dict(cls, config: dict):
        instance = cls.registry.from_dict(config)
        return cls(config=instance, type=config["type"])


@GridInitializerSelector.register("generated")
@dataclasses.dataclass
class GeneratedGridConfig(GridInitializer):
    """
    Configuration for a cubed-sphere grid computed from configuration.

    Attributes:
        stretch_factor: refinement amount
        lon_target: desired center longitude for refined tile (deg)
        lat_target: desired center latitude for refined tile (deg)
        restart_path: if given, load vertical grid from restart file
        grid_type: type of grid, 0 is a gnomonic cubed-sphere, 4 is doubly-periodic
        dx_const: constant x-width of grid cells on a dp-grid
        dy_const: constant y-width of grid cells on a dp-grid
        deglat: latitude to use for coriolis calculations on a dp-grid
    """

    stretch_factor: Optional[float] = 1.0
    lon_target: Optional[float] = 350.0
    lat_target: Optional[float] = -90.0
    restart_path: Optional[str] = None
    grid_type: Optional[int] = 0
    dx_const: Optional[float] = 1000.0
    dy_const: Optional[float] = 1000.0
    deglat: Optional[float] = 15.0
    eta_file: str = "None"

    def get_grid(
        self,
        quantity_factory: QuantityFactory,
        communicator: Communicator,
    ) -> Tuple[DampingCoefficients, DriverGridData, GridData]:
        metric_terms = MetricTerms(
            quantity_factory=quantity_factory,
            communicator=communicator,
            grid_type=self.grid_type,
            dx_const=self.dx_const,
            dy_const=self.dy_const,
            deglat=self.deglat,
            eta_file=self.eta_file,
        )
        if self.stretch_factor != 1:  # do horizontal grid transformation
            _transform_horizontal_grid(
                metric_terms, self.stretch_factor, self.lon_target, self.lat_target
            )

        horizontal_data = HorizontalGridData.new_from_metric_terms(metric_terms)
        if self.restart_path is not None:
            vertical_data = VerticalGridData.from_restart(
                self.restart_path, quantity_factory=quantity_factory
            )
        else:
            vertical_data = VerticalGridData.new_from_metric_terms(metric_terms)
        contravariant_data = ContravariantGridData.new_from_metric_terms(metric_terms)
        angle_data = AngleGridData.new_from_metric_terms(metric_terms)
        grid_data = GridData(
            horizontal_data=horizontal_data,
            vertical_data=vertical_data,
            contravariant_data=contravariant_data,
            angle_data=angle_data,
        )

        damping_coefficients = DampingCoefficients.new_from_metric_terms(metric_terms)
        driver_grid_data = DriverGridData.new_from_metric_terms(metric_terms)

        return damping_coefficients, driver_grid_data, grid_data


@GridInitializerSelector.register("serialbox")
@dataclasses.dataclass
class SerialboxGridConfig(GridInitializer):
    """
    Configuration for grid initialized from Serialbox data.
    """

    path: str

    @property
    def _f90_namelist(self) -> f90nml.Namelist:
        return f90nml.read(self.path + "/input.nml")

    @property
    def _namelist(self) -> Namelist:
        return Namelist.from_f90nml(self._f90_namelist)

    def _serializer(self, communicator: Communicator):
        import serialbox

        serializer = serialbox.Serializer(
            serialbox.OpenModeKind.Read,
            self.path,
            "Generator_rank" + str(communicator.rank),
        )
        return serializer

    def _get_serialized_grid(
        self,
        communicator: Communicator,
        backend: str,
    ) -> grid.Grid:  # type: ignore
        ser = self._serializer(communicator)
        grid = TranslateGrid.new_from_serialized_data(
            ser, communicator.rank, self._namelist.layout, backend
        ).python_grid()
        return grid

    def get_grid(
        self,
        quantity_factory: QuantityFactory,
        communicator: Communicator,
    ) -> Tuple[DampingCoefficients, DriverGridData, GridData]:
        backend = quantity_factory.zeros(
            dims=[X_DIM, Y_DIM], units="unknown"
        ).gt4py_backend

        ndsl_log.info("Using serialized grid data")
        grid = self._get_serialized_grid(communicator, backend)
        grid_data = grid.grid_data
        driver_grid_data = grid.driver_grid_data
        damping_coefficients = grid.damping_coefficients

        return damping_coefficients, driver_grid_data, grid_data


@GridInitializerSelector.register("external")
@dataclasses.dataclass
class ExternalNetcdfGridConfig(GridInitializer):
    """
    Configuration for grid initialized from external data.
    Input is from tile files generated by FRE-NCtools methods
    The ExternalNetcdfGridConfig get_grid member method calls
    the MetricTerms class method from_generated which generates
    an object of MetricTerms to be used to generate the
    damping_coefficients, driver_grid_data, and grid_data variables
    We do not read in the dx, dy, or area values as there may be
    inconsistencies in the constants used during calculation of the
    input data and the model use. An example of two adjacent finite
    volume cells in the supergrid should appear like:

    X----X----X----X----X
    |         |         |
    X    X    X    X    X
    |         |         |
    X----X----X----X----X

    The grid data must define the verticies, centroids, and mid-points
    on edge of the cells contained in the computation. For more information
    on grid discretization for the FV3 dynamical core please visit:
    https://www.gfdl.noaa.gov/fv3/
    """

    grid_type: Optional[int] = 0
    grid_file_path: str = "/input/tilefile"
    eta_file: str = "None"

    def get_grid(
        self,
        quantity_factory: QuantityFactory,
        communicator: Communicator,
    ) -> Tuple[DampingCoefficients, DriverGridData, GridData]:
        ndsl_log.info("Using external grid data")

        # ToDo: refactor when grid_type is an enum
        if self.grid_type <= 3:
            tile_num = (
                get_tile_index(communicator.rank, communicator.partitioner.total_ranks)
                + 1
            )
            tile_file = self.grid_file_path + str(tile_num) + ".nc"
        else:
            tile_file = self.grid_file_path

        ds = xr.open_dataset(tile_file)
        lon = ds.x.values
        lat = ds.y.values
        npx = ds.nxp.values.size
        npy = ds.nyp.values.size

        subtile_slice_grid = communicator.partitioner.tile.subtile_slice(
            rank=communicator.rank,
            global_dims=[Y_INTERFACE_DIM, X_INTERFACE_DIM],
            global_extent=(npy, npx),
            overlap=True,
        )

        metric_terms = MetricTerms.from_external(
            x=lon[subtile_slice_grid],
            y=lat[subtile_slice_grid],
            quantity_factory=quantity_factory,
            communicator=communicator,
            grid_type=self.grid_type,
            eta_file=self.eta_file,
        )

        horizontal_data = HorizontalGridData.new_from_metric_terms(metric_terms)
        vertical_data = VerticalGridData.new_from_metric_terms(metric_terms)
        contravariant_data = ContravariantGridData.new_from_metric_terms(metric_terms)
        angle_data = AngleGridData.new_from_metric_terms(metric_terms)
        grid_data = GridData(
            horizontal_data=horizontal_data,
            vertical_data=vertical_data,
            contravariant_data=contravariant_data,
            angle_data=angle_data,
        )

        damping_coefficients = DampingCoefficients.new_from_metric_terms(metric_terms)
        driver_grid_data = DriverGridData.new_from_metric_terms(metric_terms)

        return damping_coefficients, driver_grid_data, grid_data


def _transform_horizontal_grid(
    metric_terms: MetricTerms,
    stretch_factor: float,
    lon_target: float,
    lat_target: float,
):
    """
    Uses the Schmidt transform to locally refine the horizontal grid.

    Args:
        metric_terms
        stretch_factor: refinement factor for tile 6
        lon_target: in degrees, lon of the new center for refined tile 6
        lat_target: in degrees, lat of the new center for refined tile 6

    Returns:
        updated metric terms
    """
    grid = metric_terms.grid
    lon_transform, lat_transform = direct_transform(
        lon=grid.data[:, :, 0],
        lat=grid.data[:, :, 1],
        stretch_factor=stretch_factor,
        lon_target=lon_target,
        lat_target=lat_target,
        np=grid.np,
    )
    grid.data[:, :, 0] = lon_transform[:]
    grid.data[:, :, 1] = lat_transform[:]

    metric_terms._grid.data[:] = grid.data[:]  # type: ignore[attr-defined]
    metric_terms._init_agrid()
